"""Database access for the catalog: sites and dataset types."""

from __future__ import annotations

from sqlalchemy import Connection, func, select
from sqlalchemy.dialects.postgresql import insert

from scrapeline.catalog.datasets import DatasetRegistry
from scrapeline.catalog.sites import site_host
from scrapeline.errors import DatasetError
from scrapeline.store import tables as t


def ensure_site(conn: Connection, url_or_host: str) -> int:
    """Return the site id for a host, creating an unclassified site on first sight."""
    host = site_host(url_or_host)
    conn.execute(
        insert(t.site).values(host=host).on_conflict_do_nothing(index_elements=[t.site.c.host])
    )
    site_id: int = conn.execute(select(t.site.c.id).where(t.site.c.host == host)).scalar_one()
    return site_id


def sync_dataset_types(conn: Connection, registry: DatasetRegistry) -> int:
    """Record every dataset type. A changed definition under an existing version is refused."""
    stored = {
        (row.name, row.version): row.definition_hash
        for row in conn.execute(
            select(
                t.dataset_type.c.name, t.dataset_type.c.version, t.dataset_type.c.definition_hash
            )
        )
    }
    added = 0
    for ds in registry:
        key = (ds.name, ds.version)
        if key in stored:
            if stored[key] != ds.definition_hash:
                raise DatasetError(
                    f"dataset {ds.name} v{ds.version} changed; bump its version instead"
                )
            continue
        conn.execute(
            t.dataset_type.insert().values(
                name=ds.name,
                version=ds.version,
                pack=ds.pack,
                storage=ds.storage,
                description=ds.description,
                fields={name: f.model_dump(mode="json") for name, f in ds.fields.items()},
                key_fields=list(ds.key_fields),
                definition_hash=ds.definition_hash,
                created_at=func.now(),
            )
        )
        added += 1
    return added
