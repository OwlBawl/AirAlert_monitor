import re

phrases = [
    "[балістика] [київ]",
    "[балістика][київ]",
    "[балістика],[київ]",
    "[балістика]  [київ]",
    "балістика, київ",
    "балістика; [київ]",
    "[балістика] + [київ]"
]

for p in phrases:
    # This will match brackets, or words excluding brackets and separators
    new_tokens = re.findall(r'\[[^\]]+\]|[^\[\]\s,;+]+', p)
    print(f"Phrase: {p}")
    print(f"  Tokens: {new_tokens}")

