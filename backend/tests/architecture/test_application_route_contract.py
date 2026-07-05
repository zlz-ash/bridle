import bridle.models as production_models
from bridle.app import create_app


def _application_paths() -> set[str]:
    """Flatten normal and deferred routes; no input exits as effective application paths."""
    paths: set[str] = set()
    for route in create_app().routes:
        if hasattr(route, "path"):
            paths.add(route.path)
            continue
        router = getattr(route, "original_router", None)
        prefix = getattr(getattr(route, "include_context", None), "prefix", "")
        if router is not None:
            paths.update(f"{prefix}{child.path}" for child in router.routes)
    return paths


def test_application_exposes_only_project_runtime_routes():
    """Inspect the app route table; app input exits with only the project runtime chain exposed."""
    paths = _application_paths()

    required_paths = {
        "/api/v1/events",
        "/api/v1/health",
        "/api/v1/projects",
        "/api/v1/projects/{project_id}/map/overview",
        "/api/v1/sessions",
        "/api/v1/workspace/files",
        "/api/v1/workspace/overview",
    }
    api_roots = {path.split("/")[3] for path in paths if path.startswith("/api/v1/")}

    assert required_paths <= paths
    assert api_roots <= {"events", "health", "projects", "sessions", "workspace"}


def test_orm_metadata_exposes_only_project_runtime_tables():
    """Inspect production model exports; module input exits with project runtime records only."""
    exports = set(production_models.__all__)

    assert exports == {"Base", "ProjectRecord", "ProjectSessionRecord", "ProjectMessageRecord"}
