"""Built-in tool registrations (§7.2).

Role permissions are declared here and enforced by the registry wrapper. The
reviewer and verifier read a frozen snapshot; only the fixer may propose patches
and only writing roles may save evidence.
"""

from __future__ import annotations

from app.registry.tools import ToolRegistry, ToolSpec
from app.services.tools import artifacts_tools, checks, filesystem, outputs, patches

READ_ROLES = ("reviewer", "fixer", "verifier")
WRITE_ONLY_SCRATCH = ("verifier",)
# P10 extension sample: the test-generation role reads a frozen snapshot and
# writes only to the scratch area (never into the snapshot).
EXTENSION_READ_ROLES = READ_ROLES + ("test_generator",)
EXTENSION_SCRATCH_ROLES = WRITE_ONLY_SCRATCH + ("test_generator",)


def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()

    registry.register(
        ToolSpec(
            name="list_files",
            description="列出当前冻结快照中的文件及大小",
            args_model=filesystem.ListFilesArgs,
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema={"type": "object", "properties": {"files": {"type": "array"}}},
            timeout_seconds=5.0,
            allowed_roles=EXTENSION_READ_ROLES,
            read_only=True,
        ),
        filesystem.list_files,
    )
    registry.register(
        ToolSpec(
            name="read_file",
            description="按版本读取指定文件的文本内容",
            args_model=filesystem.ReadFileArgs,
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}, "max_bytes": {"type": "integer"}},
                "required": ["path"],
            },
            output_schema={"type": "object", "properties": {"content": {"type": "string"}}},
            timeout_seconds=5.0,
            allowed_roles=EXTENSION_READ_ROLES,
            read_only=True,
        ),
        filesystem.read_file,
    )
    registry.register(
        ToolSpec(
            name="search_code",
            description="在快照中检索代码（字面或正则）",
            args_model=filesystem.SearchCodeArgs,
            input_schema={
                "type": "object",
                "properties": {"pattern": {"type": "string"}, "regex": {"type": "boolean"}},
                "required": ["pattern"],
            },
            output_schema={"type": "object", "properties": {"matches": {"type": "array"}}},
            timeout_seconds=10.0,
            allowed_roles=EXTENSION_READ_ROLES,
            read_only=True,
        ),
        filesystem.search_code,
    )
    registry.register(
        ToolSpec(
            name="ast_check",
            description="对 Python 文件做 AST/语法检查",
            args_model=filesystem.AstCheckArgs,
            input_schema={"type": "object", "properties": {"paths": {"type": "array"}}},
            output_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
            timeout_seconds=10.0,
            allowed_roles=EXTENSION_READ_ROLES,
            read_only=True,
        ),
        filesystem.ast_check,
    )
    registry.register(
        ToolSpec(
            name="read_artifact",
            description="读取本任务已有产物（问题、补丁、验证报告、合同等）",
            args_model=artifacts_tools.ReadArtifactArgs,
            input_schema={
                "type": "object",
                "properties": {"artifact_id": {"type": "string"}},
                "required": ["artifact_id"],
            },
            output_schema={"type": "object", "properties": {"content": {"type": "string"}}},
            timeout_seconds=5.0,
            allowed_roles=EXTENSION_READ_ROLES,
            read_only=True,
        ),
        artifacts_tools.read_artifact,
    )
    registry.register(
        ToolSpec(
            name="save_evidence",
            description="保存证据或验证用测试产物到独立目录（不写入被验证快照）",
            args_model=artifacts_tools.SaveEvidenceArgs,
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}, "content": {"type": "string"}},
                "required": ["name", "content"],
            },
            output_schema={"type": "object", "properties": {"artifact_id": {"type": "string"}}},
            timeout_seconds=10.0,
            allowed_roles=EXTENSION_SCRATCH_ROLES,
            read_only=False,
        ),
        artifacts_tools.save_evidence,
    )
    registry.register(
        ToolSpec(
            name="submit_patch",
            description="提交针对当前版本的候选补丁产物（不直接应用）",
            args_model=patches.SubmitPatchArgs,
            input_schema={
                "type": "object",
                "properties": {"base_version": {"type": "string"}, "rationale": {"type": "string"}},
                "required": ["base_version", "rationale"],
            },
            output_schema={"type": "object", "properties": {"artifact_id": {"type": "string"}}},
            timeout_seconds=10.0,
            allowed_roles=("fixer",),
            read_only=False,
        ),
        patches.submit_patch,
    )
    registry.register(
        ToolSpec(
            name="run_checks",
            description="执行约定检查（syntax/static_rule/behavior_test）并返回逐项结果与证据",
            args_model=checks.RunChecksArgs,
            input_schema={
                "type": "object",
                "properties": {"checks": {"type": "array"}, "contract_version": {"type": "string"}},
                "required": ["checks", "contract_version"],
            },
            output_schema={"type": "object", "properties": {"check_results": {"type": "array"}}},
            timeout_seconds=60.0,
            allowed_roles=("reviewer", "verifier"),
            read_only=True,
        ),
        checks.run_checks,
    )
    registry.register(
        ToolSpec(
            name="submit_findings",
            description="保存审查问题（Finding）与覆盖/未检查说明",
            args_model=outputs.SubmitFindingsArgs,
            input_schema={"type": "object", "properties": {"findings": {"type": "array"}}},
            output_schema={"type": "object", "properties": {"artifact_id": {"type": "string"}}},
            timeout_seconds=10.0,
            allowed_roles=("reviewer",),
            read_only=False,
        ),
        outputs.submit_findings,
    )
    registry.register(
        ToolSpec(
            name="submit_generated_tests",
            description="保存为指定检查项生成的最小复现测试包（独立产物，供验证 Agent 使用）",
            args_model=artifacts_tools.SubmitGeneratedTestsArgs,
            input_schema={
                "type": "object",
                "properties": {"tests": {"type": "array"}},
                "required": ["tests"],
            },
            output_schema={"type": "object", "properties": {"artifact_id": {"type": "string"}}},
            timeout_seconds=10.0,
            allowed_roles=("reviewer", "test_generator"),
            read_only=False,
        ),
        artifacts_tools.submit_generated_tests,
    )
    registry.register(
        ToolSpec(
            name="submit_verification",
            description="保存验证报告（逐项检查结果、覆盖、未执行项与结论）",
            args_model=outputs.SubmitVerificationArgs,
            input_schema={"type": "object", "properties": {"check_results": {"type": "array"}}},
            output_schema={"type": "object", "properties": {"verification_id": {"type": "string"}}},
            timeout_seconds=10.0,
            allowed_roles=("verifier",),
            read_only=False,
        ),
        outputs.submit_verification,
    )
    return registry
