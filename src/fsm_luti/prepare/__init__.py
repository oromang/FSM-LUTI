"""Standalone data snapshots and explicitly controlled projection paths."""
from .regional import RegionData, ProjectionTable, load_region, prepare_region, seal_package, verify_package

__all__ = ["RegionData", "ProjectionTable", "load_region", "prepare_region", "seal_package", "verify_package"]
