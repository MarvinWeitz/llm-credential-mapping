import json
import unittest

from virtual_key_mapper.errors import CredentialError
from virtual_key_mapper.gateways.litellm import OUTPUTS
from virtual_key_mapper.labels import (
    MISSING_PHRASE, SOURCES, Attribution, Choice, Label, LabelConfig, MissingLabelsError,
    OutputOverride, compile_format, compile_formats, definitions_from_dict, env_name, launch_marker,
    parse_definitions, parse_launch_marker, validate_key, validate_value,
)

TAGS, METADATA = "x-litellm-tags", "x-litellm-spend-logs-metadata"


def central(*items):
    return parse_definitions(list(items), "rules", "test")


def personal(*items):
    return parse_definitions(list(items), "profile", "test")


def attribution(repository=None, missing=(), **values):
    return Attribution(
        tuple(Label(key, value, "file" if value else None, "x" if value else None) for key, value in values.items()),
        missing, (), repository,
    )


def config(labels=(), tags=None, metadata=None, profile=(), profile_outputs=None):
    outputs = {}
    if tags is not None:
        outputs["tags"] = OutputOverride(tuple(tags)) if tags else OutputOverride(None, False)
    if metadata is False:
        outputs["metadata"] = OutputOverride(None, False)
    return LabelConfig.build(
        OUTPUTS, central_labels=central(*labels), central_outputs=outputs,
        profile_labels=personal(*profile), profile_outputs=profile_outputs,
    )


CUSTOMER = {"key": "customer", "required": True}
PROJECT = {"key": "project"}


class ValueTests(unittest.TestCase):
    def test_exact_values_are_preserved_and_wire_constraints_are_checked(self):
        for value in ("customer:A", "Customer A", "a" * 256, "acme!&", "$x"):
            self.assertEqual(validate_value(value), value)
        for value in ("", None, [], "a" * 257, " a", "a ", "a,b", "a\nb", "a\tb", "\x00", "\x7f", "é", "!acme", "&acme"):
            with self.subTest(value=value), self.assertRaises(CredentialError):
                validate_value(value)

    def test_keys_and_their_variables(self):
        for key in ("a", "customer", "cost-center", "a_1", "a" * 32):
            self.assertEqual(validate_key(key), key)
        for key in ("", "A", "1a", "-a", "a" * 33, "a.b", "a b", None):
            with self.subTest(key=key), self.assertRaises(CredentialError):
                validate_key(key)
        self.assertEqual(env_name("cost-center"), "VKM_LABEL_COST_CENTER")

    def test_launch_marker_round_trip_and_malformed_markers_count_as_absent(self):
        marker = launch_marker({"customer": "acme", "project": ""})
        self.assertEqual(marker, '{"customer":"acme","project":null}')
        self.assertEqual(parse_launch_marker(marker), {"customer": "acme", "project": None})
        for text in (None, "", "{", "[]", '{"A": "x"}', '{"customer": "a,b"}', '{"customer": 1}', "[" * 60_000):
            with self.subTest(text=text):
                self.assertEqual(parse_launch_marker(text), {})


class DefinitionTests(unittest.TestCase):
    def test_definitions_keep_their_order_and_round_trip_without_defaults(self):
        data = [
            {"key": "customer", "required": True, "description": "Who pays", "values": [{"id": "acme", "name": "Acme"}, {"id": "globex"}],
             "strict": True, "sources": ["rules", "explicit"]},
            {"key": "project"},
        ]
        definitions = parse_definitions(data, "profile", "test")
        self.assertEqual([item.key for item in definitions], ["customer", "project"])
        self.assertEqual(definitions[0].sources, ("explicit", "rules"))
        self.assertEqual(definitions[0].choice("acme"), Choice("acme", "Acme"))
        self.assertEqual(definitions[1].sources, SOURCES)
        self.assertEqual([item.to_dict() for item in definitions], [{**data[0], "sources": ["explicit", "rules"]}, data[1]])

    def test_invalid_definitions_are_refused(self):
        for item in (
            {"key": "customer", "requried": True}, {"key": "Customer"}, {"required": True}, {"key": "a", "required": "yes"},
            {"key": "a", "strict": True}, {"key": "a", "values": []}, {"key": "a", "values": [{"id": "x"}, {"id": "x"}]},
            {"key": "a", "values": [{"id": "x", "tag": "y"}]}, {"key": "a", "values": [{"id": "a,b"}]},
            {"key": "a", "values": [{"id": "x", "name": ""}]}, {"key": "a", "values": [{"id": "x", "name": "a\nb"}]},
            {"key": "a", "values": [{"id": str(index)} for index in range(201)]},
            {"key": "a", "description": ""}, {"key": "a", "description": "x" * 201}, {"key": "a", "sources": []},
            {"key": "a", "sources": ["git"]}, {"key": "a", "sources": ["env", "env"]}, {"key": "a", "sources": "env"},
            {"key": "a", "sources": [["env"]]}, {"key": "a", "sources": [{"env": 1}]},
        ):
            with self.subTest(item=item), self.assertRaises(CredentialError):
                parse_definitions([item], "profile", "test")

    def test_keys_that_share_a_variable_conflict_and_there_are_at_most_16(self):
        with self.assertRaises(CredentialError) as error:
            parse_definitions([{"key": "cost-center"}, {"key": "cost_center"}], "profile", "test")
        self.assertIn("VKM_LABEL_COST_CENTER", str(error.exception))
        with self.assertRaises(CredentialError):
            parse_definitions([{"key": "a"}, {"key": "a"}], "profile", "test")
        parse_definitions([{"key": f"k{index}"} for index in range(16)], "profile", "test")
        with self.assertRaises(CredentialError):
            parse_definitions([{"key": f"k{index}"} for index in range(17)], "profile", "test")

    def test_definitions_document(self):
        labels, outputs = definitions_from_dict({"labels": [PROJECT], "outputs": {"metadata": {"enabled": False}}})
        self.assertEqual((labels[0].key, outputs), ("project", {"metadata": OutputOverride(None, False)}))
        for data in ([], {"labels": [], "extra": 1}, {"outputs": {"tags": {"format": "x", "enabled": False}}},
                     {"outputs": {"tags": {"enabled": True}}}, {"outputs": {"tags": {}}}, {"outputs": {"tags": {"format": []}}},
                     {"outputs": {"tags": {"format": [["x"]]}}}, {"outputs": {"tags": {"format": {"x": 1}}}},
                     {"outputs": {"tags": {"format": 7}}}):
            with self.subTest(data=data), self.assertRaises(CredentialError):
                definitions_from_dict(data)


class FormatTests(unittest.TestCase):
    def test_literals_placeholders_and_dollars(self):
        entry = compile_format("$$${customer}:${project}$$", {"customer", "project"})
        self.assertEqual(entry.render({"customer": "${project}", "project": "p"}), "$${project}:p$", "substitution is one pass")
        self.assertIsNone(entry.render({"customer": "c"}))
        self.assertEqual(entry.keys, ("customer", "project"))
        self.assertEqual(compile_format("static").render({}), "static")

    def test_format_errors(self):
        for text in (
            "", "$", "a$b", "${", "${customer", "${}", "${ customer}", "${customer }", "${Customer}", "${1a}",
            "${unknown}", "${customer}${project}", "a,b", "!${customer}", "&x", " x", "x ", "é", "x" * 513, 7,
        ):
            with self.subTest(text=text), self.assertRaises(CredentialError):
                compile_format(text, {"customer", "project"})
        compile_format("${customer}!${project}", {"customer", "project"})

    def test_entries_that_start_with_a_placeholder_start_with_the_same_one(self):
        keys = {"customer", "project"}
        compile_formats(["${customer}", "${customer}:${project}"], keys)
        compile_formats(["customer:${customer}", "project:${project}", "static"], keys)
        compile_formats(["c-${customer}", "c-${customer}:${project}", "c${customer}"], keys)
        for texts in (
            ["${customer}", "${project}"], ["${customer}", "customer:${customer}"], ["${customer}", "static"],
            ["customer:${customer}", "customer:${project}"], ["customer:${customer}", "${project}"],
            ["c:${customer}", "c:x${project}"], ["c:${customer}", "c:"], ["c-${customer}", "c${project}"],
        ):
            with self.subTest(texts=texts), self.assertRaises(CredentialError) as error:
                compile_formats(texts, keys)
            self.assertIn("could then read as another", str(error.exception))

    def test_inversion_for_catalogues(self):
        keys = {"customer", "project"}
        self.assertEqual(compile_format("customer:${customer}", keys).inversion("customer"), ("customer:", ""))
        self.assertEqual(compile_format("[${customer}]", keys).inversion("customer"), ("[", "]"))
        self.assertIsNone(compile_format("${customer}:${project}", keys).inversion("customer"))
        self.assertIsNone(compile_format("project:${project}", keys).inversion("customer"))


class ConfigTests(unittest.TestCase):
    def test_default_outputs_tag_each_label_and_send_metadata(self):
        labels = config([CUSTOMER, PROJECT])
        headers = labels.headers(attribution("github.com/acme/répo", customer="acme", project="web"))
        self.assertEqual(headers, {
            TAGS: "customer:acme,project:web",
            METADATA: '{"labels":{"customer":"acme","project":"web"},"repository":"github.com/acme/r\\u00e9po",'
                      '"sources":{"customer":"file","project":"file"}}',
        })
        self.assertEqual(labels.header_names(), (TAGS, METADATA))

    def test_optional_entries_without_a_value_are_dropped_and_an_empty_list_is_omitted(self):
        labels = config([CUSTOMER, PROJECT], tags=["${customer}", "${customer}:${project}"])
        self.assertEqual(labels.headers(attribution(customer="acme"))[TAGS], "acme")
        self.assertEqual(labels.headers(attribution(customer="acme", project="p"))[TAGS], "acme,acme:p")
        only = config([PROJECT], tags=["p-${project}"])
        self.assertEqual(only.headers(attribution(project=None)), {METADATA: '{"labels":{},"sources":{}}'})

    def test_no_labels_send_only_the_repository(self):
        self.assertEqual(config().headers(attribution("github.com/a/b")), {METADATA: '{"labels":{},"repository":"github.com/a/b","sources":{}}'})
        self.assertEqual(config(metadata=False).headers(attribution()), {})

    def test_missing_required_labels_block_with_the_fixed_phrase(self):
        with self.assertRaises(MissingLabelsError) as error:
            config([CUSTOMER]).headers(attribution(missing=("customer",), customer=None))
        self.assertTrue(str(error.exception).startswith(MISSING_PHRASE))
        self.assertIsInstance(error.exception, CredentialError)
        self.assertEqual(error.exception.keys, ("customer",))

    def test_results_are_checked_deduplicated_and_size_limited(self):
        labels = config([CUSTOMER, PROJECT], tags=["c:${customer}", "c:${customer}:${project}", "static"])
        self.assertEqual(labels.headers(attribution(customer="a", project="b"))[TAGS], "c:a,c:a:b,static")
        with self.assertRaises(CredentialError):
            labels.headers(attribution(customer="a" * 256, project="b" * 256))
        same = config([CUSTOMER, {"key": "alias", "required": True}], tags=["${customer}:${alias}", "${customer}:a"])
        self.assertEqual(same.headers(attribution(customer="a", alias="a"))[TAGS], "a:a")
        many = config([{"key": f"k{index}", "required": True} for index in range(16)])
        with self.assertRaises(CredentialError) as error:
            many.headers(attribution(**{f"k{index}": "v" * 250 for index in range(16)}))
        self.assertIn("4096", str(error.exception))

    def test_reserved_results_are_never_sent(self):
        labels = config([CUSTOMER], tags=["${customer}"])
        for value in ("default", "User-Agent: claude-cli", "user-agent: x"):
            with self.subTest(value=value), self.assertRaises(CredentialError):
                labels.headers(attribution(customer=value))
        self.assertEqual(labels.headers(attribution(customer="Default"))[TAGS], "Default")
        for tags in (["default", "c:${customer}"], ["User-Agent: x", "c:${customer}"]):
            with self.subTest(tags=tags), self.assertRaises(CredentialError):
                config([CUSTOMER], tags=tags)

    def test_a_required_label_must_reach_an_output(self):
        for tags in (["${project}"], ["${customer}:${project}"], ["static"]):
            with self.subTest(tags=tags), self.assertRaises(CredentialError) as error:
                config([CUSTOMER, PROJECT], tags=tags)
            self.assertIn("reaches no output", str(error.exception))
        config([CUSTOMER, {"key": "team", "required": True}], tags=["${customer}:${team}"])
        config([CUSTOMER], tags=[])  # Only metadata is enabled: it counts.
        with self.assertRaises(CredentialError):
            config([CUSTOMER], tags=[], metadata=False)
        config([PROJECT], tags=[], metadata=False)

    def test_problems_are_collected_into_one_error(self):
        with self.assertRaises(CredentialError) as error:
            LabelConfig.build(
                OUTPUTS, central_labels=central(CUSTOMER),
                profile_outputs={"end_user": OutputOverride(("x",)), "metadata": OutputOverride(("x",))},
            )
        message = str(error.exception)
        for expected in ("no output 'end_user'", "'metadata' takes no format"):
            self.assertIn(expected, message)

    def test_central_labels_come_first_and_profile_conflicts_are_dropped_with_a_notice(self):
        labels = LabelConfig.build(
            OUTPUTS, central_labels=central(CUSTOMER, {"key": "cost-center"}),
            profile_labels=personal({"key": "team"}, {"key": "customer"}, {"key": "cost_center"}),
        )
        self.assertEqual(labels.keys, ("customer", "cost-center", "team"))
        self.assertEqual([item.origin for item in labels.definitions], ["rules", "rules", "profile"])
        self.assertEqual(len(labels.notices), 2)
        self.assertIn("'customer' is ignored", labels.notices[0])
        with self.assertRaises(CredentialError):
            LabelConfig.build(
                OUTPUTS, central_labels=central(*({"key": f"c{index}"} for index in range(10))),
                profile_labels=personal(*({"key": f"p{index}"} for index in range(7))),
            )

    def test_a_profile_output_using_a_dropped_profile_label_is_dropped_too(self):
        labels = LabelConfig.build(
            OUTPUTS, central_labels=central(CUSTOMER, {"key": "cost-center"}), profile_labels=personal({"key": "cost_center"}),
            profile_outputs={"tags": OutputOverride(("${customer}", "${customer}:${cost_center}"))},
        )
        self.assertEqual(labels.output("tags").origin, "default")
        self.assertEqual(labels.headers(attribution(customer="a"))[TAGS], "customer:a")
        self.assertTrue(any("'tags' output is ignored" in notice and "cost_center" in notice for notice in labels.notices))
        with self.assertRaises(CredentialError):  # A key no label ever defined is still an error.
            LabelConfig.build(OUTPUTS, central_labels=central(CUSTOMER), profile_outputs={"tags": OutputOverride(("${team}",))})

    def test_central_overrides_win_and_may_only_use_central_labels(self):
        labels = LabelConfig.build(
            OUTPUTS, central_labels=central(CUSTOMER), central_outputs={"tags": OutputOverride(("${customer}",))},
            profile_labels=personal(PROJECT), profile_outputs={"tags": OutputOverride(("p:${project}",))},
        )
        self.assertEqual(labels.output("tags").origin, "rules")
        self.assertTrue(any("'tags' output is ignored" in notice for notice in labels.notices))
        self.assertEqual(labels.headers(attribution(customer="a", project="p"))[TAGS], "a")
        with self.assertRaises(CredentialError):
            LabelConfig.build(
                OUTPUTS, central_labels=central(CUSTOMER), central_outputs={"tags": OutputOverride(("${customer}-${project}",))},
                profile_labels=personal(PROJECT),
            )
        mine = LabelConfig.build(OUTPUTS, central_labels=central(CUSTOMER), profile_labels=personal(PROJECT),
                                 profile_outputs={"tags": OutputOverride(("${customer}", "${customer}/${project}"))})
        self.assertEqual(mine.output("tags").origin, "profile")
        self.assertEqual(mine.headers(attribution(customer="a", project="p"))[TAGS], "a,a/p")

    def test_inversions_need_an_enabled_single_placeholder_entry(self):
        self.assertEqual(config([CUSTOMER]).inversions("tags", "customer"), (("customer:", ""),))
        self.assertEqual(config([CUSTOMER, PROJECT], tags=["c-${customer}:${project}", "c-${customer}"]).inversions("tags", "customer"), (("c-", ""),))
        self.assertEqual(config([CUSTOMER], tags=[]).inversions("tags", "customer"), ())

    def test_describe_lists_every_label_with_names_from_values(self):
        labels = config([{**CUSTOMER, "description": "Who", "values": [{"id": "acme", "name": "Acme"}]}, PROJECT])
        described = labels.describe(attribution(customer="acme"))
        self.assertEqual(described, [
            {"key": "customer", "value": "acme", "name": "Acme", "source": "file", "detail": "x", "required": True, "description": "Who"},
            {"key": "project", "value": None, "name": None, "source": None, "detail": None, "required": False, "description": None},
        ])
        json.dumps(described)


if __name__ == "__main__":
    unittest.main()
