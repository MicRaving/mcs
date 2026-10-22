# The coding scheme

The scheme is what the coding form is made of. It is not baked into the
application: fields, their order, their levels, the conditions under which they
appear and the screening criteria all live in a scheme file, and a project
points at one.

Open the **Scheme** tab to edit it.

## Where a scheme lives

```
<the CodingStudio folder>/Schemes/<id>/
    scheme.json      the blocks, fields, kinds, conditions
    criteria.json    the screening criteria and their rules
    manifest.json    name, version, description
    bundle/          the same thing packaged for agents and other tools
```

A project names its scheme in `Project/project.json`. Two projects can share one
scheme; a project can switch to another without losing its codings, because the
codings store field *names*, and the scheme keeps those names stable.

## The pieces

| Piece | What it is |
|---|---|
| **Field** | one thing you record: a name, a type, a label, an instruction |
| **Block** | a group of fields, shown as a collapsible section of the form |
| **Kind** | a closed field's levels — the numbers and names you pick from |
| **Condition** | when a field or block appears (`show_if`) |
| **Role** | what a field *means* to the pipeline, independent of its name |
| **Criteria** | the screening criteria and the rules that pre-fill a verdict |

### Field types

`string`, `text`, `integer`, `number`, `boolean`, `list`, `object`, and the
closed types: `select` (one level), `multiselect` (several).

A closed field's levels carry a **number and a name**. The form shows them merged
(`1 - Lab`), so a coder never has to guess whether the schema wants the number or
the word. Keep the number stable once coding has started: it is what the file
stores.

### Conditions

A condition is written against other fields, for example `design == 2` or
`intervention_type in [1, 3]`. Put it on a field or on a whole block, and the
form only shows that part when the condition holds. Conditions are what keeps a
long form short: you see the questions that apply to *this* paper.

### Roles

A field's name is yours to choose; its **role** is the contract with the
analysis. The pipeline asks for *the field with the `eligible_primary` role*, not
for a field called `eligible_primary`. That is what lets a scheme rename its
fields without breaking the pipeline — and why changing a role is a bigger
decision than renaming a field.

## Editing

* **Rename** a field or a block in place.
* **Reorder** blocks by dragging the handle, and fields by dragging them within
  their block.
* **Delete** a field or block with the bin. Deleting a field that codings already
  use does not delete their data; it stops the form from showing it.
* **Add** a field, a block, a level, a condition, a criterion.
* **Screening** (the toggle at the top of the left bar) is where the inclusion
  criteria, their verdict values and their auto-approval rules are edited.

The editor refuses to save a scheme that would not render: a duplicate field
name, a condition pointing at a field that does not exist, a level without a
value. A broken *regular expression* in a rule is only a warning — that rule is
skipped, the rest still works.

## The bundle

`Schemes/<id>/bundle/` holds the scheme as machine-readable files: the scheme
itself, a JSON Schema, the prompt an agent should code with, the screening
prompt, and an OpenAPI description of the endpoints. Regenerate it after a
change so an agent and a person read the same definition.

## Things to be careful about

* **Renaming a field** breaks nothing in existing codings (the file keeps the old
  key) but the form will look empty for that field. Prefer adding a new field.
* **Changing a level's number** silently changes the meaning of every coding
  that used it.
* **Deleting a criterion** that screening already used leaves old verdicts
  orphaned. Retire criteria rather than deleting them mid-project.
