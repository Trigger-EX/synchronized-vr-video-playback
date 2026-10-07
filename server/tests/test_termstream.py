from syncvr.termstream import MAX_LINES, TermStream


def decode(*chunks, **kw):
    t = TermStream(**kw)
    for c in chunks:
        t.feed(c)
    return t


def test_plain_text_and_newlines():
    assert decode(b"one\r\ntwo\n").lines == ["one", "two", ""]


def test_colour_csi_and_osc_are_stripped():
    t = decode(b"\x1b[1;32mgreen\x1b[0m \x1b]0;title\x07done \x1b]2;t\x1b\\end")
    assert t.text() == "green done end"


def test_charset_and_private_modes_are_stripped():
    assert decode(b"\x1b(Bx\x1b[?2004hy\x1b=z").text() == "xyz"


def test_sequences_split_across_chunks():
    whole = b"a\x1b[31;1mb\x1b]0;ti\x07c\x1b]0;x\x1b\\d"
    for cut in range(len(whole)):
        assert decode(whole[:cut], whole[cut:]).text() == "abcd", cut
    assert decode(*[bytes([b]) for b in whole]).text() == "abcd"


def test_partial_utf8_waits_for_the_rest():
    data = "héllo €".encode()
    assert decode(*[bytes([b]) for b in data]).text() == "héllo €"
    assert decode(b"a\xff\xfeb").text() == "a��b"


def test_carriage_return_overwrites():
    assert decode(b"progress 10%\rprogress 99%").text() == "progress 99%"
    assert decode(b"abcdef\rxy").text() == "xycdef"


def test_backspace_and_readline_erase():
    assert decode(b"ls\b\bcat").text() == "cat"
    assert decode(b"abc\b\b\x1b[K").text() == "a"
    assert decode(b"abc\x1b[2D\x1b[1P").text() == "ac"
    assert decode(b"abc\x1b[2K").text() == ""


def test_tabs_pad_to_next_stop():
    assert decode(b"a\tb").text() == "a" + " " * 7 + "b"
    assert decode(b"12345678\tb").text() == "12345678" + " " * 8 + "b"


def test_bell_is_counted_not_shown():
    t = decode(b"a\x07b\x07")
    assert t.text() == "ab" and t.bells == 2


def test_other_control_characters_are_dropped():
    assert decode(b"a\x00\x01\x7fb").text() == "ab"


def test_line_cap_drops_oldest():
    t = decode(b"".join(b"line %d\n" % i for i in range(MAX_LINES + 50)))
    assert len(t.lines) == MAX_LINES and t.lines[-1] == "" and t.lines[-2] == "line %d" % (MAX_LINES + 49)
    assert t.dropped > 0
    t = decode(b"a\nb\nc\nd\n", max_lines=3)
    assert t.lines == ["c", "d", ""]
