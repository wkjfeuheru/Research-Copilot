"""Planner 输入输出契约（planner.v1）。

契约使用 Pydantic v2 定义，并导出独立 JSON Schema。输入只接收附件元信息，
不包含文件正文或路径；输出只描述研究意图，不对实际执行顺序作出承诺。

参考：
- Pydantic 模型与 ``extra='forbid'``：
  https://docs.pydantic.dev/latest/concepts/models/#extra-data
- Pydantic JSON Schema 导出：
  https://docs.pydantic.dev/latest/concepts/json_schema/
"""

from datetime import date
from enum import Enum
import json
from pathlib import Path
from typing import Annotated, Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

NonEmptyText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=8000),
]
ShortIdentifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$",
    ),
]


class ContractModel(BaseModel):
    """所有 Planner 契约共享的严格配置。"""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
    )


class MarketScope(str, Enum):
    """当前阶段只接受 A 股研究范围。"""

    A_SHARE = "a_share"


class AttachmentMetadata(ContractModel):
    """附件元信息；不携带文件内容或服务器路径。"""

    attachment_id: NonEmptyText
    filename: NonEmptyText
    content_type: NonEmptyText | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    description: NonEmptyText | None = None


class PlannerInput(ContractModel):
    """Planner 的独立输入。"""

    research_question: NonEmptyText
    market_scope: MarketScope = MarketScope.A_SHARE
    cutoff_date: date | None = None
    selected_skill_ids: list[NonEmptyText] = Field(default_factory=list)
    attachments: list[AttachmentMetadata] = Field(default_factory=list)
    user_notes: NonEmptyText | None = None

    @model_validator(mode="after")
    def check_unique_metadata(self) -> Self:
        _ensure_unique(self.selected_skill_ids, "selected_skill_ids")
        _ensure_unique(
            [attachment.attachment_id for attachment in self.attachments],
            "attachment_id",
        )
        return self


class ResearchObjectType(str, Enum):
    """研究对象/问题类型。"""

    STOCK = "stock"
    INDUSTRY = "industry"
    COMPANY_COMPARISON = "company_comparison"


class PlanningStatus(str, Enum):
    """规划状态。"""

    READY = "ready"
    NEEDS_CLARIFICATION = "needs_clarification"
    UNSUPPORTED = "unsupported"


class Capability(str, Enum):
    """规划步骤所需的研究能力。"""

    EVIDENCE_ACQUISITION = "evidence_acquisition"
    ANALYSIS = "analysis"
    RISK_CHECK = "risk_check"
    REPORTING = "reporting"


class MaterialType(str, Enum):
    """数据需求的资料类型。"""

    FINANCIAL_STATEMENT = "financial_statement"
    MARKET_DATA = "market_data"
    INDUSTRY_REPORT = "industry_report"
    COMPANY_ANNOUNCEMENT = "company_announcement"
    NEWS = "news"
    ATTACHMENT = "attachment"
    OTHER = "other"


class UnresolvedReferenceKind(str, Enum):
    """待解析对象的类型。"""

    STOCK_CODE = "stock_code"
    DATE = "date"
    ENTITY = "entity"
    OTHER = "other"


class UnresolvedReference(ContractModel):
    """模型无法确定、需要后续解析或澄清的对象。"""

    kind: UnresolvedReferenceKind
    reference: NonEmptyText
    reason: NonEmptyText


class ResearchScope(ContractModel):
    """研究对象与范围。"""

    object_type: ResearchObjectType
    subject: NonEmptyText
    unresolved_references: list[UnresolvedReference] = Field(default_factory=list)


class ResearchObjective(ContractModel):
    """研究目标、分析维度与显式假设。"""

    goal: NonEmptyText
    analysis_dimensions: list[NonEmptyText] = Field(default_factory=list)
    assumptions: list[NonEmptyText] = Field(default_factory=list)


class DataRequirement(ContractModel):
    """一项数据需求。"""

    requirement_id: ShortIdentifier
    description: NonEmptyText
    material_types: list[MaterialType] = Field(min_length=1)
    metrics: list[NonEmptyText] = Field(default_factory=list)
    period: NonEmptyText | None = None
    purpose: NonEmptyText


class PlanStep(ContractModel):
    """一个研究步骤；只描述意图和依赖，不代表实际执行顺序。"""

    step_id: ShortIdentifier
    intent: NonEmptyText
    capabilities: list[Capability] = Field(min_length=1)
    depends_on: list[ShortIdentifier] = Field(default_factory=list)
    data_requirement_ids: list[ShortIdentifier] = Field(default_factory=list)
    skill_ids: list[NonEmptyText] = Field(default_factory=list)


class PlannerOutput(ContractModel):
    """Planner 的独立输出。"""

    status: PlanningStatus
    research_scope: ResearchScope
    objective: ResearchObjective | None = None
    data_requirements: list[DataRequirement] = Field(default_factory=list)
    steps: list[PlanStep] = Field(default_factory=list)
    clarification_questions: list[NonEmptyText] = Field(default_factory=list)
    unsupported_reason: NonEmptyText | None = None


PLANNER_SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"


def planner_input_schema() -> dict[str, Any]:
    """返回输入 JSON Schema（可 json.dumps 的可序列化字典）。"""

    return PlannerInput.model_json_schema()


def planner_output_schema() -> dict[str, Any]:
    """返回输出 JSON Schema（可 json.dumps 的可序列化字典）。"""

    return PlannerOutput.model_json_schema()


def export_planner_schemas(
    directory: Path = PLANNER_SCHEMA_DIR,
) -> tuple[Path, Path]:
    """把输入/输出 Schema 写入文件，返回两个路径。"""

    directory.mkdir(parents=True, exist_ok=True)
    input_path = directory / "planner_input.schema.json"
    output_path = directory / "planner_output.schema.json"
    input_path.write_text(
        json.dumps(planner_input_schema(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    output_path.write_text(
        json.dumps(planner_output_schema(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return input_path, output_path


def _ensure_unique(values: list[str], field_name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} 不能包含重复值")


__all__ = [
    "AttachmentMetadata",
    "Capability",
    "ContractModel",
    "DataRequirement",
    "MarketScope",
    "MaterialType",
    "PlanStep",
    "PlannerInput",
    "PlannerOutput",
    "PlanningStatus",
    "ResearchObjectType",
    "ResearchObjective",
    "ResearchScope",
    "UnresolvedReference",
    "UnresolvedReferenceKind",
    "export_planner_schemas",
    "planner_input_schema",
    "planner_output_schema",
]
