# The paper list

The left column of the Coding tab is the project: every study the app found in
`Papers/`, with the state of its coding.

## What a row tells you

| Mark | Meaning |
|---|---|
| the number | position in the list — a stable handle for talking about a study |
| the status dot | grey: not coded; part-coded; green: coded and saved |
| `2 coders` | how many people have a coding file for that study |
| `1 E` | how many experiments the reference coding has |
| `MISSING` (amber) | contrasts without complete metrics |
| `missing` (red) | no PDF in `Papers/`, or no text in `Text/` |

The name is the **study key** — `Surname et al. (Year)`. It is the same string in
every folder, and it is what the analysis uses to join a coding to its data.

## Finding a paper

* The **search box** (`Ctrl+K`) matches the key, the title and the authors.
* The **chips** filter: *All*, *To code* (no coding of yours yet), *My codings*,
  *Missing* (papers whose metrics are incomplete).
* The **funnel** button filters by study characteristics — setting, country,
  intervention type, and so on. Useful when you want to code all the
  field experiments together.

## The paper itself

The right column shows the PDF. If the PDF is slow, corrupt, or a scan, switch to
**Text** — the extracted text the app keeps in `Text/`. The text view is also
what search-in-paper and *Mark source* work against.

The toolbar above the viewer has the page number, **Fit width**, **Mark source**,
find-in-PDF, and a button that opens the PDF in its own window (useful for
copying quotations or reading two papers side by side).

## Missing files

A study can be listed without a paper. That happens when:

* `Extraction/<Key>.json` exists but `Papers/<Key>.pdf` does not;
* the PDF is there but `Text/<Key>.txt` has not been extracted yet.

The app keeps the study in the list on purpose: a coding with no PDF is still a
coding, and hiding it would hide work. Fix it by putting the file where the key
says it should be, or by renaming the file to match the key exactly.

## Why the keys matter

`schema-module` is not the key, it is the project. The key is
`Bråten et al. (2019)`, and it appears as:

```
Papers/Bråten et al. (2019).pdf
Text/Bråten et al. (2019).txt
Extraction/Bråten et al. (2019).json
Codings/Bråten et al. (2019) - MM.json
```

Non-ASCII letters are fine. What breaks things is a difference of one character:
a straight versus a curly apostrophe, `(2019)` versus `(2019a)`, a trailing
space. When a study "disappears", compare the file names character by character.

## Working through a batch

Coder mode plus the *To code* filter is the practical loop: filter to what you
have not done, open the first row, code it, save, and the app moves to the next
field or paper. Save before you leave a paper — a tab that was closed unsaved is
still uncoded.
