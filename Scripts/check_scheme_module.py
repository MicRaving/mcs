"""Self-test for the scheme module: the library's own invariants.

Not a pytest suite - the repository has none, and the packaged app excludes
pytest.  This is a plain script with asserts, so it runs anywhere:

    python Scripts/check_scheme_module.py

It checks the *library*, not a particular scheme: the normaliser must be
idempotent, the condition evaluator must fail open, the role tables must not
collide, the legacy conversion must be lossless, and the validator must catch
every defect it claims to catch.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import project_context as PC  # noqa: E402
import scheme_legacy as LEG  # noqa: E402
import scheme_lib as SL  # noqa: E402

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  ok    {name}")
    else:
        FAILURES.append(f"{name}{(' - ' + detail) if detail else ''}")
        print(f"  FAIL  {name}{(' - ' + detail) if detail else ''}")


def section(title):
    print(f"\n{title}")


# --------------------------------------------------------------------------- #

def test_slugify():
    section("slugify")
    check("strips punctuation", SL.slugify("Sample size (N)") == "sample_size_n")
    check("lowercases", SL.slugify("N Analyzed") == "n_analyzed")
    check("digits get a prefix", SL.slugify("2020 field") == "f_2020_field")
    check("empty stays empty", SL.slugify("") == "")
    check("a custom prefix is honoured", SL.slugify("1st", "c_") == "c_1st")


def test_conditions():
    section("condition evaluation")
    values = {
        "Intervention_Type": ["inoculation", "prebunking"],
        "N_Analyzed": 120,
        "Setting": "online",
        "Empty": "",
        "Blank": None,
    }
    check("in matches a list member",
          SL.eval_condition({"field": "Intervention_Type", "op": "in",
                             "value": ["inoculation"]}, values))
    check("in rejects a non-member",
          not SL.eval_condition({"field": "Intervention_Type", "op": "in",
                                 "value": ["debunking"]}, values))
    check("not_in is the negation",
          SL.eval_condition({"field": "Intervention_Type", "op": "not_in",
                             "value": ["debunking"]}, values))
    check("equals is case-insensitive",
          SL.eval_condition({"field": "Setting", "op": "equals", "value": "ONLINE"}, values))
    check("not_empty is true for a value",
          SL.eval_condition({"field": "Setting", "op": "not_empty"}, values))
    check("not_empty is false for an empty string",
          not SL.eval_condition({"field": "Empty", "op": "not_empty"}, values))
    check("not_empty is false for None",
          not SL.eval_condition({"field": "Blank", "op": "not_empty"}, values))
    check("gt compares numbers",
          SL.eval_condition({"field": "N_Analyzed", "op": "gt", "value": 100}, values))
    check("gt rejects smaller numbers",
          not SL.eval_condition({"field": "N_Analyzed", "op": "gt", "value": 500}, values))
    check("contains matches a substring",
          SL.eval_condition({"field": "Setting", "op": "contains", "value": "nlin"}, values))

    section("condition evaluation fails open")
    check("a missing reference field shows the target",
          SL.eval_condition({"field": "Not_There", "op": "in", "value": ["x"]}, values))
    check("a blank reference value shows the target",
          SL.eval_condition({"field": "Blank", "op": "equals", "value": "x"}, values))
    check("a malformed condition shows the target",
          SL.eval_condition({"field": "Setting", "op": "nonsense", "value": 1}, values))
    check("a non-dict condition shows the target",
          SL.eval_condition("not a condition", values))
    check("a non-numeric comparison shows the target",
          SL.eval_condition({"field": "Setting", "op": "gt", "value": 5}, values))


def test_options():
    section("option normalisation")
    check("plain strings become value/label pairs",
          SL.normalise_options(["a", "b"]) == [{"value": "a", "label": "a"},
                                                {"value": "b", "label": "b"}])
    check("dicts keep their value and label",
          SL.normalise_options([{"value": 1, "label": "One"}]) ==
          [{"value": 1, "label": "One"}])
    check("a dict without a label falls back to the value",
          SL.normalise_options([{"value": 7}]) == [{"value": 7, "label": "7"}])
    check("blanks are dropped", SL.normalise_options(["", "a"]) == [{"value": "a", "label": "a"}])
    check("a non-list is empty", SL.normalise_options("nope") == [])
    check("option_values unwraps", SL.option_values([{"value": 3, "label": "x"}]) == [3])
    check("option_labels maps", SL.option_labels([{"value": 3, "label": "x"}]) == {3: "x"})


def test_roles():
    section("role tables")
    check("the alias table is one-to-one",
          len(SL.LEGACY_ROLE_ALIASES) == len(set(SL.LEGACY_ROLE_ALIASES.values())),
          "two names claim the same role")
    check("every alias target is a known role",
          set(SL.LEGACY_ROLE_ALIASES.values()) <= SL.ALL_ROLES,
          str(sorted(set(SL.LEGACY_ROLE_ALIASES.values()) - SL.ALL_ROLES)))
    check("every alias target is a study or a metric role",
          set(SL.LEGACY_ROLE_ALIASES.values()) <= (set(SL.STUDY_ROLES) | set(SL.METRIC_ROLES)))
    check("the study and metric vocabularies do not overlap",
          not (set(SL.STUDY_ROLES) & set(SL.METRIC_ROLES)),
          str(sorted(set(SL.STUDY_ROLES) & set(SL.METRIC_ROLES))))
    check("an unknown role is dropped", SL.normalise_role("not_a_role") is None)
    check("a known role survives", SL.normalise_role("eligible_primary") == "eligible_primary")


def test_normalise_is_idempotent():
    section("the normaliser is idempotent")
    legacy = _load_legacy()
    once = LEG.convert_to_normalised(legacy)
    twice = SL.normalise_scheme(once)
    check("normalise(normalise(x)) == normalise(x)", once == twice)
    check("the field count survives",
          len(twice["fields"]) == len(once["fields"]))
    check("the metric count survives",
          len(twice["metrics"]["fields"]) == len(once["metrics"]["fields"]))


def test_legacy_conversion():
    section("the legacy conversion is lossless")
    legacy = _load_legacy()
    scheme = LEG.convert_to_normalised(legacy)
    before = LEG.legacy_field_names(legacy)
    after = {f["name"] for f in SL.all_fields(scheme)}
    check("no field name is lost", not (before - after), str(sorted(before - after)[:5]))
    check("no field name is invented", not (after - before), str(sorted(after - before)[:5]))
    roles = SL.role_map(scheme)
    study = [f for f in scheme["fields"] if not f.get("role")]
    metric = [f for f in scheme["metrics"]["fields"] if not f.get("role")]
    check("every study field has a role", not study,
          str([f["name"] for f in study][:5]))
    check("every metric field has a role", not metric,
          str([f["name"] for f in metric][:5]))
    check("the roles are unique", len(roles) == len(set(roles.values())))


def test_blocks():
    section("block derivation")
    legacy = _load_legacy()
    scheme = LEG.convert_to_normalised(legacy)
    blocks = SL.derive_blocks(scheme)
    covered = [n for b in blocks for n in b["fields"]]
    every = {f["name"] for f in SL.all_fields(scheme)}
    check("every field lands in exactly one block",
          sorted(covered) == sorted(every),
          str(sorted(set(covered) ^ every)[:5]))
    check("a group block exists per group",
          len([b for b in blocks if b["kind"] == "group"]) == len(scheme["groups"]))
    check("a module block exists per module",
          len([b for b in blocks if b["kind"] == "module"]) == len(scheme["modules"]))
    module_block = next(b for b in blocks if b["kind"] == "module")
    check("a module block activates on the intervention type",
          (module_block["activate_when"] or {}).get("field") == "Intervention_Type")


def test_validator_catches_defects():
    section("the validator catches what it claims to")
    base = LEG.convert_to_normalised(_load_legacy())

    def problems(mutate):
        scheme = json.loads(json.dumps(base))
        mutate(scheme)
        return SL.validate_scheme(scheme)

    def duplicate(scheme):
        scheme["fields"].append(dict(scheme["fields"][0]))

    def role_collision(scheme):
        scheme["fields"][0]["role"] = scheme["fields"][1].get("role") or "eligible_primary"

    def unknown_target(scheme):
        scheme["fields"][0]["show_if"] = {"field": "No_Such_Field", "op": "in", "value": ["x"]}

    def cycle(scheme):
        scheme["fields"][0]["show_if"] = {"field": scheme["fields"][1]["name"],
                                          "op": "in", "value": ["x"]}
        scheme["fields"][1]["show_if"] = {"field": scheme["fields"][0]["name"],
                                          "op": "in", "value": ["x"]}

    def unknown_kind(scheme):
        scheme["fields"][0]["kind"] = "no_such_kind"

    check("a duplicate field is an error",
          any(p["code"] == "duplicate_field" for p in problems(duplicate)))
    check("a role claimed twice is an error",
          any(p["code"] == "role_collision" for p in problems(role_collision)))
    check("a condition on an unknown field is an error",
          any(p["code"] == "unknown_condition_target" for p in problems(unknown_target)))
    check("a condition cycle is an error",
          any(p["code"] == "condition_cycle" for p in problems(cycle)))
    check("an unknown kind is an error",
          any(p["code"] == "unknown_kind" for p in problems(unknown_kind)))
    check("the clean scheme has no errors",
          not [p for p in SL.validate_scheme(base) if p["level"] == "error"])


def test_visibility():
    section("visibility")
    field = {"name": "X", "show_if": {"field": "A", "op": "in", "value": ["1"]}}
    check("a field whose condition holds is visible",
          SL.field_visible(field, {"A": ["1"]}))
    check("a field whose condition fails is hidden",
          not SL.field_visible(field, {"A": ["2"]}))
    check("a field without a condition is visible", SL.field_visible({}, {}))
    required = {"name": "X", "required_if": {"field": "A", "op": "equals", "value": "1"}}
    check("a conditionally required field is optional by default",
          not SL.field_required(required, {"A": "2"}))
    check("a conditionally required field becomes required",
          SL.field_required(required, {"A": "1"}))
    check("an always-required field is required",
          SL.field_required({"required": True}, {}))
    block = {"show_if": {"field": "A", "op": "equals", "value": "1"}}
    check("a block whose condition holds is visible", SL.block_visible(block, {"A": "1"}))
    check("a block whose condition fails is hidden", not SL.block_visible(block, {"A": "2"}))
    check("a module without an activation rule is always active",
          SL.block_active({"activate_when": None}, {}))


def test_effective_values():
    section("effective values")
    values = SL.effective_values({"N": 1, "T": "a"}, {"T": "b"})
    check("the record's values are the base", values["N"] == 1)
    check("an override wins", values["T"] == "b")
    check("the record is not mutated",
          SL.effective_values({"T": "a"}, {"T": "b"}) is not {"T": "b"})


def _load_legacy():
    path = os.path.join(HERE, "coding_web", "schema.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main():
    print("scheme module self-test")
    for test in (test_slugify, test_conditions, test_options, test_roles,
                 test_normalise_is_idempotent, test_legacy_conversion, test_blocks,
                 test_validator_catches_defects, test_visibility, test_effective_values):
        test()
    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
