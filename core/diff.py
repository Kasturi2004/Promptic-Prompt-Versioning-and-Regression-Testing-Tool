"""
core/diff.py
Produces a word-level diff between two pieces of text (typically two
AI responses from different prompt versions), using Python's built-in
difflib — no extra dependencies needed.
"""

import difflib


def word_diff(old_text: str, new_text: str) -> list[dict]:
    """
    Compare old_text and new_text word-by-word.
    Returns a list of {"text": str, "type": "equal"|"added"|"removed"}
    in reading order, suitable for rendering with colored spans in the UI.

    Handles None/empty gracefully instead of crashing.
    """
    old_text = old_text or ""
    new_text = new_text or ""

    old_words = old_text.split()
    new_words = new_text.split()

    matcher = difflib.SequenceMatcher(None, old_words, new_words)
    diff_tokens = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for word in old_words[i1:i2]:
                diff_tokens.append({"text": word, "type": "equal"})
        elif tag == "delete":
            for word in old_words[i1:i2]:
                diff_tokens.append({"text": word, "type": "removed"})
        elif tag == "insert":
            for word in new_words[j1:j2]:
                diff_tokens.append({"text": word, "type": "added"})
        elif tag == "replace":
            for word in old_words[i1:i2]:
                diff_tokens.append({"text": word, "type": "removed"})
            for word in new_words[j1:j2]:
                diff_tokens.append({"text": word, "type": "added"})

    return diff_tokens


def diff_to_html(diff_tokens: list[dict]) -> str:
    """
    Render diff tokens as an HTML string with inline styles, for use with
    st.markdown(..., unsafe_allow_html=True) in the Streamlit UI.
    Removed words: red strikethrough. Added words: green background.
    """
    parts = []
    for token in diff_tokens:
        word = token["text"]
        if token["type"] == "equal":
            parts.append(word)
        elif token["type"] == "removed":
            parts.append(
                f'<span style="background-color:#ffdddd; color:#a30000; '
                f'text-decoration:line-through; padding:1px 3px; border-radius:3px;">{word}</span>'
            )
        elif token["type"] == "added":
            parts.append(
                f'<span style="background-color:#ddffdd; color:#00701a; '
                f'padding:1px 3px; border-radius:3px;">{word}</span>'
            )
    return " ".join(parts)