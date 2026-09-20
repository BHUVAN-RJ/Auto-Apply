"""The coloured resume variants: changes land inside the line's own braces,
moved bullets stay plain, and the preamble is untouched but for the additions."""
from tex import mark

PRE = "\\documentclass{article}\n\\begin{document}\n"
POST = "\\end{document}\n"


def doc(*lines: str) -> str:
    return PRE + "\n".join(lines) + "\n" + POST


def test_changed_span_is_coloured_inside_the_item():
    old = doc("\\resumeItem{\\normalsize{Designed, built, and ran a service}}")
    new = doc("\\resumeItem{\\normalsize{Shipped and ran a service}}")
    out = mark.mark(old, new, "changes")
    assert "\\resumeItem{\\normalsize{{\\color{tailoradd}Shipped} and ran a service}}" in out
    assert "\\sout" not in out
    both = mark.mark(old, new, "both")
    assert "{\\color{tailordel}\\sout{Designed, built,}} {\\color{tailoradd}Shipped}" in both


def test_moved_bullet_stays_plain_and_new_bullet_is_whole():
    a = "\\resumeItem{\\normalsize{\\href{u}{\\textbf{A:}} alpha}}"
    b = "\\resumeItem{\\normalsize{\\href{v}{\\textbf{B:}} beta}}"
    c = "\\resumeItem{\\normalsize{gamma}}"
    out = mark.mark(doc(a, b), doc(b, a, c), "both")
    assert a in out and b in out
    assert "\\resumeItem{\\normalsize{{\\color{tailoradd}gamma}}}" in out
    assert "\\sout" not in out


def test_removed_bullet_is_struck_in_both_only():
    a = "\\resumeItem{\\normalsize{alpha}}"
    b = "\\resumeItem{\\normalsize{beta}}"
    assert "alpha" not in mark.mark(doc(a, b), doc(b), "changes")
    assert "\\resumeItem{\\normalsize{{\\color{tailordel}\\sout{alpha}}}}" in mark.mark(doc(a, b), doc(b), "both")


def test_preamble_gains_only_the_additions():
    old = doc("{\\normalsize one}")
    new = doc("{\\normalsize two}")
    out = mark.mark(old, new, "changes")
    assert out.startswith("\\documentclass{article}\n" + mark.PREAMBLE + "\\begin{document}\n")
    assert "{\\normalsize {\\color{tailoradd}two}}" in out


def test_unknown_shape_is_left_alone():
    old = doc("\\section{One}")
    new = doc("\\section{Two} extra")
    assert "\\section{Two} extra" in mark.mark(old, new, "changes")
