from typing import Literal

from pydantic import BaseModel


class VMFinding(BaseModel):
    vm_id: int
    vm_name: str
    status: Literal["pass", "warn", "fail"]
    summary: str
    findings: list[dict]
    remediation: list[dict]


class WaveReport(BaseModel):
    wave_number: int
    total_vms: int
    healthy_count: int
    degraded_count: int
    failed_count: int
    executive_summary: str
    per_vm_findings: list[VMFinding]
    recommendation: Literal["proceed", "hold", "escalate"]
