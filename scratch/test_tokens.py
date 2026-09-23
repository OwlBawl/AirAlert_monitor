import re

phrases = [
    "[балістика] [київ]",
    "[балістика][київ]",
    "[балістика],[київ]",
    "[балістика]  [київ]",
    "балістика, київ",
    "балістика; [київ]"
]

for p in phrases:
    # Old way
    old_tokens = re.findall(r'\[[^\]]+\]|[^\s]+', p)
    # New way: exclude common punctuation from bare words
    new_tokens = re.findall(r'\[[^\]]+\]|[^\s,;]+', p)
    print(f"Phrase: {p}")
    print(f"  Old: {old_tokens}")
    print(f"  New: {new_tokens}")

