"""Dynamic vocabulary and channel storage with atomic JSON writes and word-boundary matching.

Supports hot-reloading, unicode word-boundary regex compilation, and tier categorization.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

logger = logging.getLogger("AirAlert.Storage")


@dataclass(frozen=True)
class KeywordMatch:
    """Result of keyword matching against message text."""

    tier: str
    matched_words: Tuple[str, ...]
    critical_words: Tuple[str, ...] = ()
    standard_words: Tuple[str, ...] = ()
    cancellation_words: Tuple[str, ...] = ()
    critical_context_words: Tuple[str, ...] = ()

    @property
    def consumed_words(self) -> Tuple[str, ...]:
        """Return every logical configured key relevant to this message."""
        return tuple(
            dict.fromkeys(
                self.cancellation_words
                + self.critical_context_words
                + self.critical_words
                + self.standard_words
            )
        )


class DynamicStore:
    """Thread-safe store for keywords and channels with atomic persistence and regex compilation."""

    def __init__(self, keywords_file: Path, channels_file: Path) -> None:
        self.keywords_file = keywords_file
        self.channels_file = channels_file

        self._lock = asyncio.Lock()

        # In-memory keyword collections
        self._critical_keywords: Set[str] = set()
        self._critical_negative: Set[str] = set()
        self._standard_keywords: Set[str] = set()
        self._standard_negative: Set[str] = set()
        self._cancellation_keywords: Set[str] = set()
        self._cancellation_negative: Set[str] = set()

        # In-memory monitored channels (stores int IDs and lowercased usernames)
        self._monitored_channels: Set[Union[int, str]] = set()

        # Precompiled single-word regex patterns
        self._critical_single_regex: Optional[re.Pattern[str]] = None
        self._standard_single_regex: Optional[re.Pattern[str]] = None
        self._cancellation_single_regex: Optional[re.Pattern[str]] = None

        # Precompiled multi-word AND criteria: list of (original_phrase, tuple of token regexes)
        self._critical_multi_patterns: List[Tuple[str, Tuple[re.Pattern[str], ...]]] = []
        self._standard_multi_patterns: List[Tuple[str, Tuple[re.Pattern[str], ...]]] = []
        self._cancellation_multi_patterns: List[Tuple[str, Tuple[re.Pattern[str], ...]]] = []

        # Precompiled regex patterns for negative stop-words
        self._critical_neg_single: Optional[re.Pattern[str]] = None
        self._critical_neg_multi: List[Tuple[str, Tuple[re.Pattern[str], ...]]] = []
        self._standard_neg_single: Optional[re.Pattern[str]] = None
        self._standard_neg_multi: List[Tuple[str, Tuple[re.Pattern[str], ...]]] = []
        self._cancellation_neg_single: Optional[re.Pattern[str]] = None
        self._cancellation_neg_multi: List[Tuple[str, Tuple[re.Pattern[str], ...]]] = []

    def _compile_tier_patterns(
        self, words: Set[str]
    ) -> Tuple[Optional[re.Pattern[str]], List[Tuple[str, Tuple[re.Pattern[str], ...]]]]:
        r"""Compile every configured JSON entry as one logical key.

        - Bare single-word keys match word starts and therefore allow suffix inflections.
        - Bracketed keys are strict on both sides.
        - Multi-token keys require every token to match anywhere in the message.
        - The returned logical-pattern list preserves the original configured key so
          matching, cooldown, consumed-message tracking, and logs all use the same key.
        """
        single_words: List[str] = []
        logical_patterns: List[Tuple[str, Tuple[re.Pattern[str], ...]]] = []

        for raw_phrase in sorted(words):
            phrase = raw_phrase.strip()
            if not phrase:
                continue

            raw_tokens = re.findall(r'\[[^\]]+\]|[^\[\]\s,;+]+', phrase)
            if not raw_tokens:
                continue

            token_regexes: List[re.Pattern[str]] = []
            for token in raw_tokens:
                is_strict = token.startswith("[") and token.endswith("]")
                content = token[1:-1].strip() if is_strict else token
                if not content:
                    continue

                inner_tokens = content.split()
                if not inner_tokens:
                    continue

                escaped_part = r"\s+".join(re.escape(part) for part in inner_tokens)
                suffix = r"(?!\w)" if is_strict else ""
                token_regexes.append(
                    re.compile(
                        r"(?<!\w)" + escaped_part + suffix,
                        flags=re.IGNORECASE | re.UNICODE,
                    )
                )

                if len(raw_tokens) == 1 and len(inner_tokens) == 1:
                    single_words.append(escaped_part + suffix)

            if token_regexes:
                logical_patterns.append((phrase, tuple(token_regexes)))

        single_regex: Optional[re.Pattern[str]] = None
        if single_words:
            pattern = r"(?<!\w)(?:" + "|".join(sorted(single_words, key=len, reverse=True)) + r")"
            single_regex = re.compile(pattern, flags=re.IGNORECASE | re.UNICODE)

        return single_regex, logical_patterns

    def _atomic_write_json(self, file_path: Path, data: Any) -> None:
        """Atomically persist JSON data via temporary file rename to prevent file corruption."""
        file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = file_path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            tmp_path.replace(file_path)
        except Exception as exc:
            logger.error("Failed to write JSON atomically to %s: %s", file_path, exc, exc_info=True)
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise

    async def load_all(self) -> None:
        """Load keywords and channels from disk and recompile regex filters."""
        async with self._lock:
            self._load_keywords_sync()
            self._load_channels_sync()

    def _recompile_patterns(self) -> None:
        """Recompile all regex patterns for positive and negative keywords across all tiers."""
        self._critical_single_regex, self._critical_multi_patterns = self._compile_tier_patterns(
            self._critical_keywords
        )
        self._critical_neg_single, self._critical_neg_multi = self._compile_tier_patterns(
            self._critical_negative
        )

        self._standard_single_regex, self._standard_multi_patterns = self._compile_tier_patterns(
            self._standard_keywords
        )
        self._standard_neg_single, self._standard_neg_multi = self._compile_tier_patterns(
            self._standard_negative
        )

        self._cancellation_single_regex, self._cancellation_multi_patterns = self._compile_tier_patterns(
            self._cancellation_keywords
        )
        self._cancellation_neg_single, self._cancellation_neg_multi = self._compile_tier_patterns(
            self._cancellation_negative
        )

    def _save_keywords_sync(self) -> None:
        """Synchronously persist keyword sets to disk atomically."""
        data = {
            "critical": sorted(list(self._critical_keywords)),
            "critical_negative": sorted(list(self._critical_negative)),
            "standard": sorted(list(self._standard_keywords)),
            "standard_negative": sorted(list(self._standard_negative)),
            "cancellation": sorted(list(self._cancellation_keywords)),
            "cancellation_negative": sorted(list(self._cancellation_negative)),
        }
        self._atomic_write_json(self.keywords_file, data)

    def _load_keywords_sync(self) -> None:
        """Synchronously parse keywords file and compile regex patterns."""
        if not self.keywords_file.exists():
            default_keywords = {
                "critical": [],
                "critical_negative": [],
                "standard": [],
                "standard_negative": [],
                "cancellation": [],
                "cancellation_negative": [],
            }
            self._atomic_write_json(self.keywords_file, default_keywords)

        try:
            with open(self.keywords_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            self._critical_keywords = {
                w.strip().lower() for w in data.get("critical", []) if isinstance(w, str) and w.strip()
            }
            self._critical_negative = {
                w.strip().lower() for w in data.get("critical_negative", []) if isinstance(w, str) and w.strip()
            }
            self._standard_keywords = {
                w.strip().lower() for w in data.get("standard", []) if isinstance(w, str) and w.strip()
            }
            self._standard_negative = {
                w.strip().lower() for w in data.get("standard_negative", []) if isinstance(w, str) and w.strip()
            }
            self._cancellation_keywords = {
                w.strip().lower() for w in data.get("cancellation", []) if isinstance(w, str) and w.strip()
            }
            self._cancellation_negative = {
                w.strip().lower() for w in data.get("cancellation_negative", []) if isinstance(w, str) and w.strip()
            }

            self._recompile_patterns()

            logger.info(
                "Loaded %d crit (+%d neg), %d std (+%d neg), %d cancel (+%d neg) keywords.",
                len(self._critical_keywords),
                len(self._critical_negative),
                len(self._standard_keywords),
                len(self._standard_negative),
                len(self._cancellation_keywords),
                len(self._cancellation_negative),
            )
        except Exception as exc:
            logger.error("Error reading keywords from %s: %s", self.keywords_file, exc, exc_info=True)

    def _load_channels_sync(self) -> None:
        """Synchronously parse channels file."""
        if not self.channels_file.exists():
            default_channels = {"channels": []}
            self._atomic_write_json(self.channels_file, default_channels)

        try:
            with open(self.channels_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            raw_channels = data.get("channels", [])
            parsed_channels: Set[Union[int, str]] = set()

            for item in raw_channels:
                if isinstance(item, int):
                    parsed_channels.add(item)
                elif isinstance(item, str):
                    cleaned = item.strip().lower()
                    if cleaned:
                        parsed_channels.add(cleaned)

            self._monitored_channels = parsed_channels
            logger.info("Loaded %d monitored channels.", len(self._monitored_channels))
        except Exception as exc:
            logger.error("Error reading channels from %s: %s", self.channels_file, exc, exc_info=True)

    async def add_keyword(self, word: str, tier: str = "standard") -> bool:
        """Add a keyword or stop-word (with '-' prefix) to critical, standard, or cancellation list."""
        cleaned = word.strip().lower()
        if not cleaned:
            return False

        is_negative = cleaned.startswith("-")
        lookup_word = cleaned[1:].strip() if is_negative else cleaned
        if not lookup_word:
            return False

        tier = tier.lower()
        if tier not in ("critical", "standard", "cancellation"):
            tier = "standard"

        async with self._lock:
            if tier == "critical":
                target_set = self._critical_negative if is_negative else self._critical_keywords
            elif tier == "cancellation":
                target_set = self._cancellation_negative if is_negative else self._cancellation_keywords
            else:
                target_set = self._standard_negative if is_negative else self._standard_keywords

            if lookup_word in target_set:
                return False  # Already exists

            # If positive keyword, remove from other positive tiers to maintain clear tiering
            if not is_negative:
                if tier == "critical":
                    self._standard_keywords.discard(lookup_word)
                    self._cancellation_keywords.discard(lookup_word)
                elif tier == "cancellation":
                    self._critical_keywords.discard(lookup_word)
                    self._standard_keywords.discard(lookup_word)
                else:
                    self._critical_keywords.discard(lookup_word)
                    self._cancellation_keywords.discard(lookup_word)

            target_set.add(lookup_word)
            self._recompile_patterns()
            self._save_keywords_sync()
            return True

    async def remove_keyword(self, word: str, tier: str = "standard") -> bool:
        """Remove a keyword or stop-word (with '-' prefix) strictly from the specified tier."""
        cleaned = word.strip().lower()
        if not cleaned:
            return False

        is_negative = cleaned.startswith("-")
        lookup_word = cleaned[1:].strip() if is_negative else cleaned
        if not lookup_word:
            return False

        tier = tier.lower()
        if tier not in ("critical", "standard", "cancellation"):
            tier = "standard"

        async with self._lock:
            if tier == "critical":
                target_set = self._critical_negative if is_negative else self._critical_keywords
            elif tier == "cancellation":
                target_set = self._cancellation_negative if is_negative else self._cancellation_keywords
            else:
                target_set = self._standard_negative if is_negative else self._standard_keywords

            if lookup_word not in target_set:
                return False  # Not found in this specific tier

            target_set.remove(lookup_word)
            self._recompile_patterns()
            self._save_keywords_sync()
            return True

    async def get_keywords(self) -> Dict[str, List[str]]:
        """Return snapshot of current keywords and negative words by tier."""
        async with self._lock:
            return {
                "critical": sorted(list(self._critical_keywords)),
                "critical_negative": sorted(list(self._critical_negative)),
                "standard": sorted(list(self._standard_keywords)),
                "standard_negative": sorted(list(self._standard_negative)),
                "cancellation": sorted(list(self._cancellation_keywords)),
                "cancellation_negative": sorted(list(self._cancellation_negative)),
            }

    async def add_channel(self, channel_identifier: Union[int, str]) -> bool:
        """Add channel by numeric ID or username string."""
        if isinstance(channel_identifier, str):
            clean_str = channel_identifier.strip().lower()
            if not clean_str:
                return False
            # Try to convert numeric string to integer
            if clean_str.lstrip("-").isdigit():
                clean_target: Union[int, str] = int(clean_str)
            else:
                clean_target = clean_str
        else:
            clean_target = channel_identifier

        async with self._lock:
            if clean_target in self._monitored_channels:
                return False

            self._monitored_channels.add(clean_target)
            data = {"channels": sorted(list(self._monitored_channels), key=lambda x: str(x))}
            self._atomic_write_json(self.channels_file, data)
            return True

    async def remove_channel(self, channel_identifier: Union[int, str]) -> bool:
        """Remove channel by numeric ID or username string."""
        clean_target: Union[int, str]
        if isinstance(channel_identifier, str):
            clean_str = channel_identifier.strip().lower()
            if clean_str.lstrip("-").isdigit():
                clean_target = int(clean_str)
            else:
                clean_target = clean_str
        else:
            clean_target = channel_identifier

        async with self._lock:
            if clean_target not in self._monitored_channels:
                # Also try matching with/without '@'
                if isinstance(clean_target, str):
                    alt = clean_target[1:] if clean_target.startswith("@") else f"@{clean_target}"
                    if alt in self._monitored_channels:
                        clean_target = alt
                    else:
                        return False
                else:
                    return False

            self._monitored_channels.discard(clean_target)
            data = {"channels": sorted(list(self._monitored_channels), key=lambda x: str(x))}
            self._atomic_write_json(self.channels_file, data)
            return True

    async def get_channels(self) -> List[Union[int, str]]:
        """Return list of monitored channels."""
        async with self._lock:
            return sorted(list(self._monitored_channels), key=lambda x: str(x))

    def is_channel_monitored(self, chat_id: int, username: Optional[str] = None) -> bool:
        """Check if incoming chat matches any configured channel identifier (by ID or username)."""
        if chat_id in self._monitored_channels:
            return True

        if username:
            clean_user = username.strip().lower()
            if clean_user in self._monitored_channels:
                return True
            with_at = f"@{clean_user}"
            if with_at in self._monitored_channels:
                return True

        return False

    @staticmethod
    def _has_pattern_match(
        text: str,
        single_rx: Optional[re.Pattern[str]],
        multi_pts: List[Tuple[str, Tuple[re.Pattern[str], ...]]],
    ) -> bool:
        """Return True if text matches either single regex or all tokens in any multi-word pattern."""
        if single_rx is not None and single_rx.search(text):
            return True
        for _phrase, token_regexes in multi_pts:
            if all(rx.search(text) for rx in token_regexes):
                return True
        return False

    @staticmethod
    def _find_positive_matches(
        text: str,
        _single_rx: Optional[re.Pattern[str]],
        logical_patterns: List[Tuple[str, Tuple[re.Pattern[str], ...]]],
    ) -> Tuple[str, ...]:
        """Return all matching logical JSON keys for one keyword group."""
        return tuple(
            phrase
            for phrase, token_regexes in logical_patterns
            if all(rx.search(text) for rx in token_regexes)
        )

    def match_text(self, text: Optional[str]) -> Optional[KeywordMatch]:
        """Collect all valid logical keys, with negatives scoped to their own group."""
        if not text:
            return None

        critical_matches = self._find_positive_matches(
            text, self._critical_single_regex, self._critical_multi_patterns
        )
        standard_matches = self._find_positive_matches(
            text, self._standard_single_regex, self._standard_multi_patterns
        )
        cancellation_matches = self._find_positive_matches(
            text, self._cancellation_single_regex, self._cancellation_multi_patterns
        )

        critical_blocked = bool(critical_matches) and self._has_pattern_match(
            text, self._critical_neg_single, self._critical_neg_multi
        )
        standard_blocked = bool(standard_matches) and self._has_pattern_match(
            text, self._standard_neg_single, self._standard_neg_multi
        )
        cancellation_blocked = bool(cancellation_matches) and self._has_pattern_match(
            text, self._cancellation_neg_single, self._cancellation_neg_multi
        )

        valid_critical = () if critical_blocked else critical_matches
        valid_standard = () if standard_blocked else standard_matches
        valid_cancellation = () if cancellation_blocked else cancellation_matches

        # A valid cancellation has message-level precedence. Critical negatives
        # belong only to the active critical group; positive critical keys still
        # identify whether the cancelled threat is critical.
        if valid_cancellation:
            critical_context = critical_matches
            tier = "cancellation_critical" if critical_context else "cancellation_standard"
            return KeywordMatch(
                tier=tier,
                matched_words=valid_cancellation,
                critical_words=valid_critical,
                standard_words=valid_standard,
                cancellation_words=valid_cancellation,
                critical_context_words=critical_context,
            )

        if valid_critical or valid_standard:
            tier = "critical" if valid_critical else "standard"
            matched_words = valid_critical if valid_critical else valid_standard
            return KeywordMatch(
                tier=tier,
                matched_words=matched_words,
                critical_words=valid_critical,
                standard_words=valid_standard,
            )

        return None
