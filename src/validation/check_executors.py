"""Validation check type executors.

SECURITY: This module executes validation commands and checks file operations.
All subprocess calls use shell=False with argument lists to prevent command injection.
"""

import shlex
import subprocess
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional


class ValidationCheckType(Enum):
    """Types of validation checks."""

    FILE_EXISTS = "file_exists"
    FILE_CONTAINS = "file_contains"
    COMMAND_SUCCESS = "command_success"
    MANUAL_VERIFICATION = "manual_verification"
    CODE_REVIEW = "code_review"
    TEST_PASS = "test_pass"
    PERFORMANCE_METRIC = "performance_metric"


# SECURITY: Patterns that indicate dangerous commands
DANGEROUS_PATTERNS = [
    ";",
    "&&",
    "||",
    "|",
    "$(",
    "`",
    "${",
    "rm -rf",
    "mkfs",
    "dd if=",
    "/etc/passwd",
    "/etc/shadow",
    "sudo",
    "su -",
    "chmod 777",
    "eval ",
    "exec ",
    "curl |",
    "wget |",
    "python -c",
    "python3 -c",
]


def _validate_command_safety(command: str) -> Optional[str]:
    """Validate that a command doesn't contain dangerous patterns.

    Args:
        command: Command string to validate

    Returns:
        Error message if dangerous, None if safe
    """
    command_lower = command.lower().strip()

    for pattern in DANGEROUS_PATTERNS:
        if pattern in command_lower:
            return f"Command rejected: contains dangerous pattern '{pattern}'"

    # Additional checks
    if not command.strip():
        return "Empty command"

    return None


def execute_validation_check(
    check_type: ValidationCheckType, criterion: Dict[str, Any], working_directory: str
) -> Dict[str, Any]:
    """Execute a validation check based on its type.

    Args:
        check_type: Type of validation check
        criterion: Check criterion configuration
        working_directory: Agent's working directory

    Returns:
        Dict with check result and evidence
    """
    if check_type == ValidationCheckType.FILE_EXISTS:
        return _check_file_exists(criterion, working_directory)

    elif check_type == ValidationCheckType.FILE_CONTAINS:
        return _check_file_contains(criterion, working_directory)

    elif check_type == ValidationCheckType.COMMAND_SUCCESS:
        return _check_command_success(criterion, working_directory)

    elif check_type == ValidationCheckType.TEST_PASS:
        return _check_test_pass(criterion, working_directory)

    elif check_type == ValidationCheckType.MANUAL_VERIFICATION:
        return {
            "passed": None,  # Validator must determine
            "evidence": "Manual verification required",
            "requires_manual": True,
        }

    elif check_type == ValidationCheckType.CODE_REVIEW:
        return {
            "passed": None,  # Validator must determine
            "evidence": f"Code review required for areas: {criterion.get('focus_areas', [])}",
            "requires_manual": True,
        }

    elif check_type == ValidationCheckType.PERFORMANCE_METRIC:
        return {
            "passed": None,  # Validator must measure
            "evidence": f"Performance metric check: {criterion.get('metric', 'unknown')}",
            "requires_manual": True,
        }

    else:
        return {
            "passed": None,
            "evidence": f"Unknown check type: {check_type}",
            "error": True,
        }


def _check_file_exists(criterion: Dict[str, Any], working_dir: str) -> Dict[str, Any]:
    """Check if files exist.

    Args:
        criterion: Check criterion with 'target' field
        working_dir: Working directory

    Returns:
        Check result
    """
    targets = criterion.get("target", [])
    if isinstance(targets, str):
        targets = [targets]

    results = []
    all_exist = True

    for target in targets:
        # SECURITY: Validate target path doesn't escape working directory
        file_path = (Path(working_dir) / target).resolve()
        if not str(file_path).startswith(str(Path(working_dir).resolve())):
            results.append(f"{target}: PATH TRAVERSAL BLOCKED")
            all_exist = False
            continue

        exists = file_path.exists()
        results.append(f"{target}: {'EXISTS' if exists else 'MISSING'}")
        if not exists:
            all_exist = False

    return {
        "passed": all_exist,
        "evidence": "\n".join(results),
        "files_checked": targets,
    }


def _check_file_contains(criterion: Dict[str, Any], working_dir: str) -> Dict[str, Any]:
    """Check if file contains pattern.

    Args:
        criterion: Check criterion with 'target' and 'pattern' fields
        working_dir: Working directory

    Returns:
        Check result
    """
    target = criterion.get("target", "")
    patterns = criterion.get("pattern", [])
    if isinstance(patterns, str):
        patterns = [patterns]

    # SECURITY: Validate target path doesn't escape working directory
    file_path = (Path(working_dir) / target).resolve()
    if not str(file_path).startswith(str(Path(working_dir).resolve())):
        return {
            "passed": False,
            "evidence": f"Path traversal blocked for: {target}",
            "error": True,
            "security_rejection": True,
        }

    if not file_path.exists():
        return {
            "passed": False,
            "evidence": f"File {target} does not exist",
            "error": True,
        }

    try:
        content = file_path.read_text()
        results = []
        all_found = True

        for pattern in patterns:
            found = pattern in content
            results.append(f"Pattern '{pattern}': {'FOUND' if found else 'NOT FOUND'}")
            if not found:
                all_found = False

        return {
            "passed": all_found,
            "evidence": "\n".join(results),
            "patterns_checked": patterns,
        }

    except Exception as e:
        return {
            "passed": False,
            "evidence": f"Error reading file: {str(e)}",
            "error": True,
        }


def _check_command_success(
    criterion: Dict[str, Any], working_dir: str
) -> Dict[str, Any]:
    """Check if command executes successfully.

    SECURITY: Uses shell=False with argument list to prevent command injection.
    Commands are validated against dangerous patterns before execution.

    Args:
        criterion: Check criterion with 'command' field
        working_dir: Working directory

    Returns:
        Check result
    """
    command = criterion.get("command", "")
    if not command:
        return {"passed": False, "evidence": "No command specified", "error": True}

    # SECURITY: Validate command safety
    safety_error = _validate_command_safety(command)
    if safety_error:
        return {
            "passed": False,
            "evidence": safety_error,
            "error": True,
            "security_rejection": True,
        }

    try:
        # SECURITY: Use shell=False with argument list (shlex.split)
        cmd_args = shlex.split(command)

        if not cmd_args:
            return {
                "passed": False,
                "evidence": "Empty command after parsing",
                "error": True,
            }

        result = subprocess.run(
            cmd_args,
            shell=False,  # SECURITY: Never use shell=True
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=30,
        )

        passed = result.returncode == 0

        return {
            "passed": passed,
            "evidence": f"Command: {command}\nExit Code: {result.returncode}\nOutput: {result.stdout}\nError: {result.stderr}",
            "exit_code": result.returncode,
        }

    except subprocess.TimeoutExpired:
        return {
            "passed": False,
            "evidence": f"Command timed out after 30 seconds: {command}",
            "error": True,
        }

    except Exception as e:
        return {
            "passed": False,
            "evidence": f"Error running command: {str(e)}",
            "error": True,
        }


def _check_test_pass(criterion: Dict[str, Any], working_dir: str) -> Dict[str, Any]:
    """Check if tests pass.

    SECURITY: Uses shell=False with argument list to prevent command injection.
    Test commands are validated against dangerous patterns before execution.

    Args:
        criterion: Check criterion with 'command' field
        working_dir: Working directory

    Returns:
        Check result
    """
    test_command = criterion.get("command", "")
    if not test_command:
        return {"passed": False, "evidence": "No test command specified", "error": True}

    # SECURITY: Validate command safety
    safety_error = _validate_command_safety(test_command)
    if safety_error:
        return {
            "passed": False,
            "evidence": f"Test command rejected: {safety_error}",
            "error": True,
            "security_rejection": True,
        }

    try:
        # SECURITY: Use shell=False with argument list (shlex.split)
        cmd_args = shlex.split(test_command)

        if not cmd_args:
            return {
                "passed": False,
                "evidence": "Empty command after parsing",
                "error": True,
            }

        result = subprocess.run(
            cmd_args,
            shell=False,  # SECURITY: Never use shell=True
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=120,  # Tests may take longer
        )

        passed = result.returncode == 0

        # Parse test output if possible
        output = result.stdout + result.stderr
        test_summary = _extract_test_summary(output)

        return {
            "passed": passed,
            "evidence": f"Test Command: {test_command}\nExit Code: {result.returncode}\n{test_summary}\nFull Output:\n{output[:2000]}",
            "exit_code": result.returncode,
            "test_summary": test_summary,
        }

    except subprocess.TimeoutExpired:
        return {
            "passed": False,
            "evidence": f"Tests timed out after 120 seconds: {test_command}",
            "error": True,
        }

    except Exception as e:
        return {
            "passed": False,
            "evidence": f"Error running tests: {str(e)}",
            "error": True,
        }


def _extract_test_summary(output: str) -> str:
    """Extract test summary from output.

    Args:
        output: Test output string

    Returns:
        Summary string
    """
    lines = output.strip().split("\n")

    # Common test framework patterns
    for line in reversed(lines):
        # pytest summary
        if "passed" in line and "failed" in line:
            return line.strip()
        # Jest summary
        if "Tests:" in line:
            return line.strip()
        # Generic test count
        if any(x in line.lower() for x in ["test", "spec", "assertion"]):
            return line.strip()

    # Return last non-empty line as fallback
    for line in reversed(lines):
        if line.strip():
            return line.strip()

    return "No summary found"
