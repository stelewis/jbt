"""An entry point whose import would violate selected-only loading."""

message = "unselected synthetic importer was loaded"
raise RuntimeError(message)
