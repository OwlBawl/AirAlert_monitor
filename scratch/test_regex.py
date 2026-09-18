import re

def compile_phrase(phrase):
    single_words = []
    multi_patterns = []
    
    raw_tokens = re.findall(r'\[[^\]]+\]|[^\s]+', phrase)
    if not raw_tokens:
        return None
    
    if len(raw_tokens) == 1:
        token = raw_tokens[0]
        is_strict = token.startswith("[") and token.endswith("]")
        content = token[1:-1].strip() if is_strict else token
        inner_tokens = content.split()
        
        if len(inner_tokens) == 1:
            escaped_word = re.escape(inner_tokens[0])
            if is_strict:
                single_words.append(escaped_word + r"(?!\w)")
            else:
                single_words.append(escaped_word)
        else:
            escaped_phrase = r"\s+".join(re.escape(t) for t in inner_tokens)
            multi_patterns.append(
                (
                    phrase,
                    (
                        re.compile(
                            r"(?<!\w)" + escaped_phrase + (r"(?!\w)" if is_strict else ""),
                            flags=re.IGNORECASE | re.UNICODE,
                        ),
                    ),
                )
            )
    else:
        token_regexes = []
        for token in raw_tokens:
            is_strict = token.startswith("[") and token.endswith("]")
            content = token[1:-1].strip() if is_strict else token
            if not content:
                continue
            
            inner_tokens = content.split()
            if not inner_tokens:
                continue
                
            escaped_part = r"\s+".join(re.escape(t) for t in inner_tokens)
            if is_strict:
                token_regexes.append(
                    re.compile(r"(?<!\w)" + escaped_part + r"(?!\w)", flags=re.IGNORECASE | re.UNICODE)
                )
            else:
                token_regexes.append(
                    re.compile(r"(?<!\w)" + escaped_part, flags=re.IGNORECASE | re.UNICODE)
                )
        
        if token_regexes:
            multi_patterns.append((phrase, tuple(token_regexes)))
            
    return single_words, multi_patterns

test_phrases = [
    "[балістика київ]",
    "[балістика] [київ]",
    "балістика київ"
]

for p in test_phrases:
    sw, mp = compile_phrase(p)
    print(f"Phrase: {p}")
    print(f"Single words: {sw}")
    print(f"Multi patterns: {[[rx.pattern for rx in tpl] for _, tpl in mp]}")
    print("-" * 20)

