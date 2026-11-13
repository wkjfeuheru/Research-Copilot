import pytest


def test_literal_percentage_format_error_is_caught_without_executing_code():
    from research_copilot.sandbox.execution_policy import validate_python_source

    with pytest.raises(ValueError, match="line 1"):
        validate_python_source('description = "growth 37.15%); value %.2f" % value')


def test_valid_escaped_percent_and_fstrings_are_accepted():
    from research_copilot.sandbox.execution_policy import validate_python_source

    validate_python_source('description = "growth 37.15%%; value %.2f" % value')
    validate_python_source('description = f"growth 37.15%; value {value:.2f}"')


def test_source_is_parsed_but_never_executed():
    from research_copilot.sandbox.execution_policy import validate_python_source

    validate_python_source('raise RuntimeError("must not execute on host")')
    with pytest.raises(ValueError, match="syntax"):
        validate_python_source("if True print(1)")
