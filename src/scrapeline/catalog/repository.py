"""Database access for the catalog: sites and dataset types."""

from __future__ import annotations

from sqlalchemy import Connection, func, select
from sqlalchemy.dialects.postgresql import insert

from scrapeline.catalog.datasets import DatasetRegistry, DatasetType
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


def ensure_dataset_type(conn: Connection, ds: DatasetType) -> bool:
    """Record one dataset type if it is new; True when it was added.

    Raises DatasetError when the stored definition of the same version differs.
    """
    added = conn.execute(
        insert(t.dataset_type)
        .values(
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
        .on_conflict_do_nothing(index_elements=[t.dataset_type.c.name, t.dataset_type.c.version])
        .returning(t.dataset_type.c.name)
    ).first()
    if added is not None:
        return True
    stored = conn.execute(
        select(t.dataset_type.c.definition_hash).where(
            t.dataset_type.c.name == ds.name, t.dataset_type.c.version == ds.version
        )
    ).scalar_one()
    if stored != ds.definition_hash:
        raise DatasetError(f"dataset {ds.name} v{ds.version} changed; bump its version instead")
    return False


def sync_dataset_types(conn: Connection, registry: DatasetRegistry) -> int:
    """Record every dataset type. A changed definition under an existing version is refused."""
    return sum(ensure_dataset_type(conn, ds) for ds in registry)
