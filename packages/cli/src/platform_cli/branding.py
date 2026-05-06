"""Small branding model for standalone CLI copy."""

from __future__ import annotations

import os

from pydantic import BaseModel, Field


class Branding(BaseModel):
    """User-facing CLI branding loaded from environment variables."""

    product_name: str = Field(
        default_factory=lambda: os.getenv("BRAND_PRODUCT_NAME", "General Augment"),
    )
    product_slug: str = Field(
        default_factory=lambda: os.getenv("BRAND_PRODUCT_SLUG", "general-augment"),
    )
    docs_url: str = Field(
        default_factory=lambda: os.getenv("BRAND_DOCS_URL", "https://docs.generalaugment.com"),
    )
    api_key_prefix: str = Field(default_factory=lambda: os.getenv("BRAND_API_KEY_PREFIX", "gaadm"))


def get_branding() -> Branding:
    """Return current process branding."""
    return Branding()
