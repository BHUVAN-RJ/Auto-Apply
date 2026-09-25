"""Any LaTeX resume can be tailored: the tailor reads the person's layout
instead of requiring its own. Four common designs, read and validated."""

import pytest

from tailor import structure, tailor

JAKE = r"""\documentclass[letterpaper,11pt]{article}
\newcommand{\resumeItem}[1]{\item\small{{#1 \vspace{-2pt}}}}
\begin{document}
\begin{center}\textbf{\Huge Sam Lee} \\ sam@example.com\end{center}
\section{Education}
\resumeSubheading{State University}{City}{B.S. Computer Science}{2024}
\section{Experience}
\resumeSubHeadingListStart
\resumeSubheading{Acme}{2024 -- Present}{Software Engineer}{Remote}
\resumeItemListStart
  \resumeItem{Built a billing service in Go that settles 2M invoices a day}
  \resumeItem{Cut deploy time from 40 to 12 minutes with a cached CI pipeline}
\resumeItemListEnd
\resumeSubHeadingListEnd
\section{Projects}
\resumeItemListStart
  \resumeItem{\textbf{Tracer} | Python: a request tracer for Flask apps}
\resumeItemListEnd
\section{Technical Skills}
\begin{itemize}[leftmargin=0.15in, label={}]
\small{\item{
 \textbf{Languages}{: Go, Python, SQL} \\
 \textbf{Tools}{: Docker, Kubernetes, Git}
}}
\end{itemize}
\end{document}
"""

PLAIN = r"""\documentclass{article}
\usepackage{enumitem}
\begin{document}
{\Large Ana Ruiz}\\ ana@example.com
\section*{Profile}
Backend engineer who ships reliable services in Java and Kotlin.
\section*{Work Experience}
\textbf{Initech}, Software Engineer \hfill 2022--2025
\begin{itemize}[noitemsep]
  \item Designed the payments ledger in Kotlin, handling 300 requests a second
  \item Mentored two interns through their first production launch
\end{itemize}
\section*{Education}
B.Sc. Computer Science, Tech University, 2022
\section*{Skills}
Languages: Java, Kotlin, SQL\\
Infrastructure: AWS, Terraform
\end{document}
"""

AWESOME = r"""\documentclass[11pt, a4paper]{awesome-cv}
\begin{document}
\makecvheader
\cvsection{Experience}
\begin{cventries}
  \cventry{Data Engineer}{Globex}{Berlin}{2023 - Now}{
    \begin{cvitems}
      \item {Moved 40 nightly jobs from cron to Airflow, cutting failed runs by half}
      \item {Built a dbt model layer used by six analytics teams}
    \end{cvitems}
  }
\end{cventries}
\cvsection{Skills}
\begin{cvskills}
  \cvskill{Data}{Airflow, dbt, Spark}
\end{cvskills}
\cvsection{Education}
\cventry{M.Sc.}{TU Berlin}{Berlin}{2023}{}
\end{document}
"""

BOLD = r"""\documentclass{article}
\begin{document}
Kim Park -- kim@example.com

\textbf{EXPERIENCE}\\[2pt]
Hooli, Engineer, 2021--2024
\begin{itemize}
  \item Scaled the search indexer to 5 billion documents
  \item Rewrote the ranking service in Rust, halving p99 latency
\end{itemize}

\textbf{EDUCATION}\\[2pt]
B.S., Cal, 2021
\end{document}
"""


@pytest.mark.parametrize("tex, experience, bullets", [
    (JAKE, "Experience", 3),
    (PLAIN, "Work Experience", 2),
    (AWESOME, "Experience", 2),
    (BOLD, "EXPERIENCE", 2),
])
def test_every_layout_is_read(tex, experience, bullets):
    assert structure.titles(tex)["EXPERIENCE"] == experience
    assert len(tailor.bullets(tex)) == bullets
    assert tailor.master_problems(tex) == []


def test_roles_are_found_by_their_many_names():
    found = structure.titles(PLAIN)
    assert found["SUMMARY"] == "Profile" and found["TECHNICAL SKILLS"] == "Skills"
    assert "EDUCATION" in tailor.split_sections(PLAIN)
    assert tailor._summary(PLAIN).strip().startswith("Backend engineer")


def test_skills_lines_in_each_shape():
    assert len(tailor._skill_lines(JAKE)) == 2
    assert tailor._skill_lines(PLAIN) == ["Languages: Java, Kotlin, SQL\\\\",
                                          "Infrastructure: AWS, Terraform"]


def test_item_bullets_stop_at_the_next_item_and_the_list_end():
    assert tailor.bullets(PLAIN) == [
        "Designed the payments ledger in Kotlin, handling 300 requests a second",
        "Mentored two interns through their first production launch",
    ]


def test_a_rewritten_bullet_passes_in_any_layout():
    tailored = PLAIN.replace("Mentored two interns through their first production launch",
                             "Mentored two interns through their first launch to production")
    tailor._validate(PLAIN, tailored)


def test_a_frozen_section_stays_frozen_in_any_layout():
    tailored = PLAIN.replace("Tech University", "Tech Institute")
    with pytest.raises(tailor.TailorError, match="EDUCATION"):
        tailor._validate(PLAIN, tailored)


def test_the_design_is_restored_above_the_first_heading():
    tailored = AWESOME.replace(r"\makecvheader", r"\makecvheader[C]")
    restored, changed = tailor.restore_preamble(AWESOME, tailored)
    assert changed and restored == AWESOME


def test_a_resume_with_no_experience_heading_is_named():
    tex = "\\documentclass{article}\\begin{document}\\section{Hobbies}x\\section{Travel}y\\end{document}"
    problems = tailor.master_problems(tex)
    assert problems and '"Hobbies"' in problems[0]


def test_the_model_is_told_the_resumes_own_headings():
    note = tailor.layout_note(PLAIN)
    assert 'EXPERIENCE: the section headed "Work Experience"' in note
    assert "PROJECTS: this resume has none" in note
    assert '"Education"' in note and "`\\item` lines" in note
