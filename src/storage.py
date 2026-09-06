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

    tier: str  # "critical" or "standard"
    matched_words: Tuple[str, ...]


class DynamicStore:
    """Thread-safe store for keywords and channels with atomic persistence and regex compilation."""

    def __init__(self, keywords_file: Path, channels_file: Path) -> None:
        self.keywords_file = keywords_file
        self.channels_file = channels_file

        self._lock = asyncio.Lock()

        # In-memory keyword collections
        self._critical_keywords: Set[str] = set()
        self._standard_keywords: Set[str] = set()

        # In-memory monitored channels (stores int IDs and lowercased usernames)
        self._monitored_channels: Set[Union[int, str]] = set()

        # Precompiled single-word regex patterns
        self._critical_single_regex: Optional[re.Pattern[str]] = None
        self._standard_single_regex: Optional[re.Pattern[str]] = None

        # Precompiled multi-word AND criteria: list of (original_phrase, tuple of token regexes)
        self._critical_multi_patterns: List[Tuple[str, Tuple[re.Pattern[str], ...]]] = []
        self._standard_multi_patterns: List[Tuple[str, Tuple[re.Pattern[str], ...]]] = []

    def _compile_tier_patterns(
        self, words: Set[str]
    ) -> Tuple[Optional[re.Pattern[str]], List[Tuple[str, Tuple[re.Pattern[str], ...]]]]:
        r"""Separate single-word and multi-word keys.
        
        - Single-word keys use word-start boundary only (?<!\w)word — allowing suffix inflections.
        - Multi-word keys (e.g. 'баліст київ') require all tokens/stems to match anywhere in the text.
        """
        single_words: List[str] = []
        multi_patterns: List[Tuple[str, Tuple[re.Pattern[str], ...]]] = []

        for raw_phrase in words:
            phrase = raw_phrase.strip()
            if not phrase:
                continue
            tokens = phrase.split()
            if len(tokens) == 1:
                single_words.append(re.escape(tokens[0]))
            else:
                # Compile regex for each token in multi-word key.
                # Use word-start boundary (?<!\w) allowing inflections/suffixes on stems
                token_regexes = tuple(
                    re.compile(r"(?<!\w)" + re.escape(t), flags=re.IGNORECASE | re.UNICODE)
                    for t in tokens
                    if t
                )
                if token_regexes:
                    multi_patterns.append((phrase, token_regexes))

        single_regex: Optional[re.Pattern[str]] = None
        if single_words:
            # Sort longer words first
            sorted_words = sorted(single_words, key=len, reverse=True)
            # Word-start boundary only: stems like "бандерол" match inflected forms
            # ("Бандероль", "Бандеролі") — no right boundary (?!\w) restriction
            pattern = r"(?<!\w)(?:" + "|".join(sorted_words) + r")"
            single_regex = re.compile(pattern, flags=re.IGNORECASE | re.UNICODE)

        return single_regex, multi_patterns

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

    def _load_keywords_sync(self) -> None:
        """Synchronously parse keywords file and compile regex patterns."""
        if not self.keywords_file.exists():
            default_keywords = {"critical": [], "standard": []}
            self._atomic_write_json(self.keywords_file, default_keywords)

        try:
            with open(self.keywords_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            self._critical_keywords = {
                w.strip().lower() for w in data.get("critical", []) if isinstance(w, str) and w.strip()
            }
            self._standard_keywords = {
                w.strip().lower() for w in data.get("standard", []) if isinstance(w, str) and w.strip()
            }

            self._critical_single_regex, self._critical_multi_patterns = self._compile_tier_patterns(
                self._critical_keywords
            )
            self._standard_single_regex, self._standard_multi_patterns = self._compile_tier_patterns(
                self._standard_keywords
            )

            logger.info(
                "Loaded %d critical and %d standard keywords.",
                len(self._critical_keywords),
                len(self._standard_keywords),
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
        """Add a keyword to either critical or standard list dynamically."""
        cleaned = word.strip().lower()
        if not cleaned:
            return False

        tier = tier.lower()
        if tier not in ("critical", "standard"):
            tier = "standard"

        async with self._lock:
            target_set = self._critical_keywords if tier == "critical" else self._standard_keywords
            if cleaned in target_set:
                return False  # Already exists

            # Remove from opposite tier if present to maintain clear tiering
            if tier == "critical":
                self._standard_keywords.discard(cleaned)
                self._critical_keywords.add(cleaned)
            else:
                self._critical_keywords.discard(cleaned)
                self._standard_keywords.add(cleaned)

            self._critical_single_regex, self._critical_multi_patterns = self._compile_tier_patterns(
                self._critical_keywords
            )
            self._standard_single_regex, self._standard_multi_patterns = self._compile_tier_patterns(
                self._standard_keywords
            )

            data = {
                "critical": sorted(list(self._critical_keywords)),
                "standard": sorted(list(self._standard_keywords)),
            }
            self._atomic_write_json(self.keywords_file, data)
            return True

    async def remove_keyword(self, word: str) -> bool:
        """Remove a keyword from whatever tier it exists in."""
        cleaned = word.strip().lower()
        if not cleaned:
            return False

        async with self._lock:
            in_crit = cleaned in self._critical_keywords
            in_std = cleaned in self._standard_keywords

            if not in_crit and not in_std:
                return False

            self._critical_keywords.discard(cleaned)
            self._standard_keywords.discard(cleaned)

            self._critical_single_regex, self._critical_multi_patterns = self._compile_tier_patterns(
                self._critical_keywords
            )
            self._standard_single_regex, self._standard_multi_patterns = self._compile_tier_patterns(
                self._standard_keywords
            )

            data = {
                "critical": sorted(list(self._critical_keywords)),
                "standard": sorted(list(self._standard_keywords)),
            }
            self._atomic_write_json(self.keywords_file, data)
            return True

    async def get_keywords(self) -> Dict[str, List[str]]:
        """Return snapshot of current keywords by tier."""
        async with self._lock:
            return {
                "critical": sorted(list(self._critical_keywords)),
                "standard": sorted(list(self._standard_keywords)),
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

    def match_text(self, text: Optional[str]) -> Optional[KeywordMatch]:
        """Evaluate text against compiled regexes and multi-word patterns, prioritizing critical tier."""
        if not text:
            return None

        # 1. Check CRITICAL tier first
        # 1a. Critical multi-word patterns (all words/stems must be present in text)
        for phrase, token_regexes in self._critical_multi_patterns:
            if all(rx.search(text) for rx in token_regexes):
                return KeywordMatch(tier="critical", matched_words=(phrase,))

        # 1b. Critical single-word regex
        if self._critical_single_regex is not None:
            matches = self._critical_single_regex.findall(text)
            if matches:
                unique_matches = tuple(dict.fromkeys(m.lower() for m in matches))
                return KeywordMatch(tier="critical", matched_words=unique_matches)

        # 2. Check STANDARD tier
        # 2a. Standard multi-word patterns (all words/stems must be present in text)
        for phrase, token_regexes in self._standard_multi_patterns:
            if all(rx.search(text) for rx in token_regexes):
                return KeywordMatch(tier="standard", matched_words=(phrase,))

        # 2b. Standard single-word regex
        if self._standard_single_regex is not None:
            matches = self._standard_single_regex.findall(text)
            if matches:
                unique_matches = tuple(dict.fromkeys(m.lower() for m in matches))
                return KeywordMatch(tier="standard", matched_words=unique_matches)

        return None
