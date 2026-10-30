from src.main import chunk_text


def sentence(number: int) -> str:
    return f"Sentence number {number} explains one idea in a few plain words."


def test_short_text_is_one_chunk_with_normalised_whitespace():
    assert chunk_text("  Pods   are\n\nsmall.  ") == ["Pods are small."]


def test_blank_text_has_no_chunks():
    assert chunk_text(" \n\n ") == []


def test_splits_between_paragraphs():
    first = " ".join(sentence(n) for n in range(15))
    second = " ".join(sentence(n) for n in range(100, 115))

    chunks = chunk_text(f"{first}\n\n{second}")

    assert chunks == [first, second]


def test_splits_after_sentences():
    chunks = chunk_text(" ".join(sentence(n) for n in range(100)))

    assert len(chunks) > 1
    assert all(chunk.startswith("Sentence number") for chunk in chunks)
    assert all(chunk.endswith(".") for chunk in chunks)


def test_single_newlines_do_not_split_sentences():
    # HTML extraction puts a newline around every inline tag.
    text = " ".join(f"Sentence number {n} uses\nthe\nword inline." for n in range(100))

    chunks = chunk_text(text)

    assert len(chunks) > 1
    assert all(chunk.startswith("Sentence number") for chunk in chunks)
    assert all(chunk.endswith(".") for chunk in chunks)


def test_chunks_stay_within_max_chars_and_overlap():
    chunks = chunk_text(" ".join(sentence(n) for n in range(100)))

    assert all(len(chunk) <= 1400 for chunk in chunks)
    assert all(chunk[:40] in previous for previous, chunk in zip(chunks, chunks[1:]))


def test_text_without_spaces_is_still_split():
    chunks = chunk_text("x" * 3000)

    assert len(chunks) > 1
    assert all(len(chunk) <= 1400 for chunk in chunks)
