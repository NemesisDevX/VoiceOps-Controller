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
