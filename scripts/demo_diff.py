from core.diff import word_diff, diff_to_html


def main():
    old = "The stock market fell 3% today because of inflation fears."
    new = "The stock market declined today because of inflation."

    tokens = word_diff(old, new)
    for token in tokens:
        print(f"[{token['type']:7}] {token['text']}")

    print("\nHTML output:")
    print(diff_to_html(tokens))


if __name__ == "__main__":
    main()
