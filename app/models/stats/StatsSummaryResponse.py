from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import date


class TrashCompositionItem(BaseModel):
    """Single trash type with its count and percentage."""
    label: str
    count: int
    percentage: float = Field(ge=0, le=100)


class EnvironmentalFootprint(BaseModel):
    """Total environmental impact metrics."""
    total_area_sqm: float = Field(ge=0, description="Cumulative area in square meters")
    total_detections: int = Field(ge=0, description="Total number of detections")


class WorkerEfficiency(BaseModel):
    """Efficiency metrics for a single AI worker."""
    name: str
    success_count: int = Field(ge=0)
    failure_count: int = Field(ge=0)
    tasks_processed_today: int = Field(ge=0)
    reliability_score: float = Field(ge=0, le=1, description="Success rate (0-1)")


class AIFleetEfficiency(BaseModel):
    """Fleet-wide efficiency metrics."""
    workers: List[WorkerEfficiency]
    fleet_reliability_score: float = Field(ge=0, le=1)
    total_successes: int = Field(ge=0)
    total_failures: int = Field(ge=0)


class TemporalTrend(BaseModel):
    """Daily trash report count."""
    date: date
    count: int = Field(ge=0)


class ProcessingTime(BaseModel):
    """Processing time by worker."""
    worker_name: str
    avg_processing_seconds: float = Field(ge=0)
    task_count: int = Field(ge=0)


class MeanTimeToProcess(BaseModel):
    """Mean time to process metrics."""
    overall_avg_seconds: float = Field(ge=0)
    by_worker: List[ProcessingTime]


class HotspotDensity(BaseModel):
    """High-confidence trash hotspot metrics."""
    hotspot_count: int = Field(ge=0, description="Number of high-priority zones")
    high_confidence_media_count: int = Field(ge=0, description="Media items with >=80% confidence")


class TrashCompositionResponse(BaseModel):
    """Response wrapper for trash composition endpoint."""
    items: List[TrashCompositionItem]
    total_detections: int = Field(ge=0)


class TemporalTrendsResponse(BaseModel):
    """Response wrapper for temporal trends endpoint."""
    trends: List[TemporalTrend]
    days_window: int = Field(description="Time window in days")


class StatsSummaryResponse(BaseModel):
    """Complete statistics summary response."""
    trash_composition: List[TrashCompositionItem]
    environmental_footprint: EnvironmentalFootprint
    ai_fleet_efficiency: AIFleetEfficiency
    temporal_trends: List[TemporalTrend]
    mean_time_to_process: MeanTimeToProcess
    hotspot_density: HotspotDensity
    days_window: int = Field(description="Time window in days used for temporal data")
