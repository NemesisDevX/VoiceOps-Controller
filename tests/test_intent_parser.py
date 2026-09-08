"""Unit tests for deterministic stateful intent parsing and pronoun resolution."""

from __future__ import annotations

from app.schemas.command import InspectionTarget, IntentType
from app.schemas.telemetry import ProcessInfo
from app.services.intent_parser import IntentParser


def test_parses_memory_inspection() -> None:
    parser = IntentParser()
    result = parser.parse("show me the top memory processes")

    assert result.intent is IntentType.INSPECT
    assert result.inspection_target is InspectionTarget.MEMORY
    assert result.requires_confirmation is False


def test_parses_cpu_inspection() -> None:
    parser = IntentParser()
    result = parser.parse("what are the top cpu processes right now")

    assert result.intent is IntentType.INSPECT
    assert result.inspection_target is InspectionTarget.CPU


def test_parses_kill_with_explicit_pid() -> None:
    parser = IntentParser()
    result = parser.parse("kill pid 4242")

    assert result.intent is IntentType.PROCESS_KILL
    assert result.target_pid == 4242
    assert result.requires_confirmation is True
    assert result.resolved_from_context is False


def test_parses_kill_with_process_name() -> None:
    parser = IntentParser()
    result = parser.parse("terminate process named nginx")

    assert result.intent is IntentType.PROCESS_KILL
    assert result.target_process_name == "nginx"
    assert result.requires_confirmation is True


def test_parses_isolate_with_ip_address() -> None:
    parser = IntentParser()
    result = parser.parse("isolate 10.0.0.5 from the network")

    assert result.intent is IntentType.NETWORK_ISOLATE
    assert result.target_ip == "10.0.0.5"
    assert result.requires_confirmation is True


def test_unknown_intent_for_unrecognized_text() -> None:
    parser = IntentParser()
    result = parser.parse("good morning, how are you")

    assert result.intent is IntentType.UNKNOWN
    assert result.confidence == 0.0


def test_empty_transcript_is_unknown() -> None:
    parser = IntentParser()
    result = parser.parse("   ")

    assert result.intent is IntentType.UNKNOWN


def test_stateful_pronoun_resolution_resolves_to_top_inspection_subject(sample_processes: list[ProcessInfo]) -> None:
    parser = IntentParser()

    inspection = parser.parse("show me the top memory processes")
    parser.remember_inspection(inspection.inspection_target, sample_processes)

    follow_up = parser.parse("kill it")

    assert follow_up.intent is IntentType.PROCESS_KILL
    assert follow_up.resolved_from_context is True
    assert follow_up.target_pid == sample_processes[0].pid
    assert follow_up.target_process_name == sample_processes[0].name
    assert follow_up.requires_confirmation is True


def test_pronoun_resolution_variants_all_resolve(sample_processes: list[ProcessInfo]) -> None:
    parser = IntentParser()
    inspection = parser.parse("list top memory processes")
    parser.remember_inspection(inspection.inspection_target, sample_processes)

    for phrase in ("kill that process", "terminate the top one", "stop that one"):
        result = parser.parse(phrase)
        assert result.resolved_from_context is True
        assert result.target_pid == sample_processes[0].pid


def test_pronoun_without_prior_context_does_not_resolve() -> None:
    parser = IntentParser()
    result = parser.parse("kill it")

    assert result.intent is IntentType.PROCESS_KILL
    assert result.resolved_from_context is False
    assert result.target_pid is None


def test_context_is_isolated_per_parser_instance(sample_processes: list[ProcessInfo]) -> None:
    parser_a = IntentParser()
    parser_b = IntentParser()

    inspection = parser_a.parse("show top memory processes")
    parser_a.remember_inspection(inspection.inspection_target, sample_processes)

    result_from_b = parser_b.parse("kill it")

    assert result_from_b.resolved_from_context is False


# --- Multilingual lexicon matrix ------------------------------------------------------------


def test_arabic_process_kill_sets_language() -> None:
    parser = IntentParser()
    result = parser.parse("اقفل العملية 4242")

    assert result.intent is IntentType.PROCESS_KILL
    assert result.language == "ar"
    assert result.target_pid == 4242
    assert result.requires_confirmation is True


def test_arabic_network_isolate_sets_language() -> None:
    parser = IntentParser()
    result = parser.parse("اعزل 10.0.0.5")

    assert result.intent is IntentType.NETWORK_ISOLATE
    assert result.language == "ar"
    assert result.target_ip == "10.0.0.5"


def test_arabic_inspect_sets_language() -> None:
    parser = IntentParser()
    result = parser.parse("افحص الذاكرة")

    assert result.intent is IntentType.INSPECT
    assert result.language == "ar"


def test_arabic_rollback_sets_language() -> None:
    parser = IntentParser()
    result = parser.parse("ارجع التعديل الاخير")

    assert result.intent is IntentType.ROLLBACK
    assert result.language == "ar"


def test_spanish_process_kill_sets_language() -> None:
    parser = IntentParser()
    result = parser.parse("terminar pid 4242")

    assert result.intent is IntentType.PROCESS_KILL
    assert result.language == "es"
    assert result.target_pid == 4242


def test_spanish_network_isolate_sets_language() -> None:
    parser = IntentParser()
    result = parser.parse("aislar 10.0.0.5")

    assert result.intent is IntentType.NETWORK_ISOLATE
    assert result.language == "es"
    assert result.target_ip == "10.0.0.5"


def test_spanish_inspect_sets_language() -> None:
    parser = IntentParser()
    result = parser.parse("mostrar la memoria")

    assert result.intent is IntentType.INSPECT
    assert result.language == "es"


def test_spanish_rollback_sets_language() -> None:
    parser = IntentParser()
    result = parser.parse("revertir el cambio")

    assert result.intent is IntentType.ROLLBACK
    assert result.language == "es"


def test_french_process_kill_sets_language() -> None:
    parser = IntentParser()
    result = parser.parse("arrêter pid 4242")

    assert result.intent is IntentType.PROCESS_KILL
    assert result.language == "fr"
    assert result.target_pid == 4242


def test_french_network_isolate_sets_language() -> None:
    parser = IntentParser()
    result = parser.parse("isoler 10.0.0.5")

    assert result.intent is IntentType.NETWORK_ISOLATE
    assert result.language == "fr"
    assert result.target_ip == "10.0.0.5"


def test_french_inspect_sets_language() -> None:
    parser = IntentParser()
    result = parser.parse("afficher la mémoire")

    assert result.intent is IntentType.INSPECT
    assert result.language == "fr"


def test_french_rollback_sets_language() -> None:
    parser = IntentParser()
    result = parser.parse("annuler le changement")

    assert result.intent is IntentType.ROLLBACK
    assert result.language == "fr"


def test_chinese_process_kill_substring_matches_without_spaces() -> None:
    # Note: a space is kept before the PID digits because `_BARE_NUMBER_PATTERN`'s `\b` boundary
    # (correctly, per requirement 6) does not fire between two adjacent Unicode "word" characters
    # such as a Chinese ideograph directly abutting a digit; the *verb* itself ("终止") is still
    # matched purely by substring containment with no surrounding whitespace required.
    parser = IntentParser()
    result = parser.parse("终止 4242")

    assert result.intent is IntentType.PROCESS_KILL
    assert result.language == "zh"
    assert result.target_pid == 4242


def test_chinese_network_isolate_sets_language() -> None:
    parser = IntentParser()
    result = parser.parse("隔离 10.0.0.5")

    assert result.intent is IntentType.NETWORK_ISOLATE
    assert result.language == "zh"
    assert result.target_ip == "10.0.0.5"


def test_chinese_inspect_substring_matches_without_spaces() -> None:
    parser = IntentParser()
    result = parser.parse("检查内存状态")

    assert result.intent is IntentType.INSPECT
    assert result.language == "zh"


def test_chinese_rollback_sets_language() -> None:
    parser = IntentParser()
    result = parser.parse("回滚上次更改")

    assert result.intent is IntentType.ROLLBACK
    assert result.language == "zh"


def test_arabic_substring_matches_within_a_longer_phrase() -> None:
    """Arabic has ASCII-visible spaces but no ASCII word-boundary semantics; verify the verb
    is still found when glued to surrounding words that a naive `\\b` regex might mis-bound."""
    parser = IntentParser()
    result = parser.parse("من فضلك اقفل العملية رقم 4242 الان")

    assert result.intent is IntentType.PROCESS_KILL
    assert result.language == "ar"
    assert result.target_pid == 4242


# --- Fuzzy resolution of acoustic slips ------------------------------------------------------


def test_fuzzy_slip_kilit_resolves_to_process_kill() -> None:
    parser = IntentParser()
    result = parser.parse("kilit pid 4242")

    assert result.intent is IntentType.PROCESS_KILL
    assert result.language == "en"
    assert result.target_pid == 4242
    assert result.requires_confirmation is True


def test_fuzzy_slip_stopp_resolves_to_process_kill() -> None:
    parser = IntentParser()
    result = parser.parse("stopp pid 4242")

    assert result.intent is IntentType.PROCESS_KILL
    assert result.target_pid == 4242


def test_fuzzy_slip_terminador_resolves_to_spanish_process_kill() -> None:
    parser = IntentParser()
    result = parser.parse("terminador pid 4242")

    assert result.intent is IntentType.PROCESS_KILL
    assert result.language == "es"
    assert result.target_pid == 4242


def test_fuzzy_matching_never_crosses_arabic_or_chinese_scripts() -> None:
    """A Latin-script fuzzy slip must not spuriously match Arabic/Chinese lexicon entries, and
    Arabic/Chinese text must not be run through the Latin `difflib` fuzzy path at all."""
    parser = IntentParser()
    result = parser.parse("kilit العملية")

    assert result.intent is IntentType.PROCESS_KILL
    assert result.language == "en"


# --- Stateful multilingual pronoun resolution -------------------------------------------------


def test_arabic_fused_kill_it_pronoun_resolves_from_context(sample_processes: list[ProcessInfo]) -> None:
    parser = IntentParser()
    inspection = parser.parse("افحص الذاكرة")
    parser.remember_inspection(inspection.inspection_target, sample_processes)

    follow_up = parser.parse("اقفله")

    assert follow_up.intent is IntentType.PROCESS_KILL
    assert follow_up.language == "ar"
    assert follow_up.resolved_from_context is True
    assert follow_up.target_pid == sample_processes[0].pid
    assert follow_up.requires_confirmation is True


def test_spanish_fused_kill_it_pronoun_resolves_from_context(sample_processes: list[ProcessInfo]) -> None:
    parser = IntentParser()
    inspection = parser.parse("mostrar la memoria")
    parser.remember_inspection(inspection.inspection_target, sample_processes)

    follow_up = parser.parse("terminarlo")

    assert follow_up.intent is IntentType.PROCESS_KILL
    assert follow_up.language == "es"
    assert follow_up.resolved_from_context is True
    assert follow_up.target_pid == sample_processes[0].pid


def test_french_fused_kill_it_pronoun_resolves_from_context(sample_processes: list[ProcessInfo]) -> None:
    parser = IntentParser()
    inspection = parser.parse("afficher la mémoire")
    parser.remember_inspection(inspection.inspection_target, sample_processes)

    follow_up = parser.parse("arrête-le")

    assert follow_up.intent is IntentType.PROCESS_KILL
    assert follow_up.language == "fr"
    assert follow_up.resolved_from_context is True
    assert follow_up.target_pid == sample_processes[0].pid


def test_chinese_fused_kill_it_pronoun_resolves_from_context(sample_processes: list[ProcessInfo]) -> None:
    parser = IntentParser()
    inspection = parser.parse("检查内存")
    parser.remember_inspection(inspection.inspection_target, sample_processes)

    follow_up = parser.parse("把它关掉")

    assert follow_up.intent is IntentType.PROCESS_KILL
    assert follow_up.language == "zh"
    assert follow_up.resolved_from_context is True
    assert follow_up.target_pid == sample_processes[0].pid


def test_arabic_standalone_pronoun_resolves_alongside_explicit_verb(sample_processes: list[ProcessInfo]) -> None:
    """Non-fused pronoun phrase ("this process") combined with a separately-matched verb."""
    parser = IntentParser()
    inspection = parser.parse("افحص الذاكرة")
    parser.remember_inspection(inspection.inspection_target, sample_processes)

    follow_up = parser.parse("اقفل هذه العملية")

    assert follow_up.intent is IntentType.PROCESS_KILL
    assert follow_up.resolved_from_context is True
    assert follow_up.target_pid == sample_processes[0].pid


def test_multilingual_pronoun_without_prior_context_does_not_resolve() -> None:
    parser = IntentParser()
    result = parser.parse("arrête-le")

    assert result.intent is IntentType.PROCESS_KILL
    assert result.resolved_from_context is False
    assert result.target_pid is None


def test_multilingual_context_is_isolated_per_parser_instance(sample_processes: list[ProcessInfo]) -> None:
    parser_a = IntentParser()
    parser_b = IntentParser()

    inspection = parser_a.parse("mostrar la memoria")
    parser_a.remember_inspection(inspection.inspection_target, sample_processes)

    result_from_b = parser_b.parse("terminarlo")

    assert result_from_b.resolved_from_context is False


# --- Unrecognized phrases across every supported language stay UNKNOWN ----------------------


def test_unrecognized_phrase_is_unknown_in_every_supported_language() -> None:
    parser = IntentParser()

    for phrase in (
        "good morning, how are you",
        "صباح الخير كيف حالك",
        "buenos días, cómo estás",
        "bonjour, comment ça va",
        "早上好,你好吗",
    ):
        result = parser.parse(phrase)
        assert result.intent is IntentType.UNKNOWN, phrase
        assert result.confidence == 0.0
