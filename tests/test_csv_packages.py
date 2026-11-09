from __future__ import annotations

from rankrecovery.core.ranks import default_policy
from rankrecovery.services.csvimport import rank_from_package_text

TEBEX = (
    b"ID,Transaction ID,Date,Status,Gateway,Username,Uuid,Price,Tax,Currency,Name,Notes,Country,Email,IP,Project,Coupon,Gift,Packages,Categories,Custom,Amount,Chargeback,Refund\n"
    b"79400451,tbx-43125224a5054-21b1e0,2024-09-09T01:24:14+00:00,Complete,free,CJ_ALT1,7285321aab574b70a62234ea17297e4d,0.00,0.00,USD,\"Curtis N\",,US,x@example.com,1.2.3.4,6107228,10724302,,\"'Galactic to Infinite'\",\"'Rank Upgrades'\",,0.00,,\n"
    b"79399868,tbx-63725224a3413-d527e8,2024-09-09T00:56:54+00:00,Declined,globalcards,CJ_ALT1,7285321aab574b70a62234ea17297e4d,35.00,0.00,USD,\"A Gift\",,US,x@example.com,1.2.3.4,6107228,,,\"'Galactic to Infinite'\",\"'Rank Upgrades'\",,35.00,,\n"
    b"79300000,tbx-11111111a1111-aaaaaa,2024-05-01T05:29:13+00:00,Complete,paypal,Player4995,78114d30-4e24-4d6d-8384-33e6bc41bed6,20.00,0.00,USD,\"P\",,US,p@example.com,1.2.3.4,6107228,,,\"'Immortal Rank', 'Fly Perk'\",\"'Ranks', 'Perks'\",,20.00,,\n"
    b"79300001,tbx-22222222a2222-bbbbbb,2024-05-02T05:29:13+00:00,Complete,paypal,Mani_Festive,d0f2fa97-222a-4a09-a3d3-2b8d5a336310,5.00,0.00,USD,\"M\",,US,m@example.com,1.2.3.4,6107228,,,\"'Mystery Crate'\",\"'Crates'\",,5.00,,\n"
)


def test_auto_map_prefers_columns_by_their_values():
    from rankrecovery.services.csvimport import CsvImportService

    headers = ["ID", "Transaction ID", "Date", "Status", "Username", "Email", "Uuid", "Package IDs", "Package Names"]
    rows = [
        {"ID": "79400451", "Transaction ID": "tbx-1", "Date": "2024-09-09T01:24:14+00:00", "Status": "Complete", "Username": "CJ_ALT1", "Email": "x@example.com", "Uuid": "7285321aab574b70a62234ea17297e4d", "Package IDs": "6107228", "Package Names": "'Galactic to Infinite'"},
        {"ID": "79400452", "Transaction ID": "tbx-2", "Date": "2024-09-10T01:24:14+00:00", "Status": "Declined", "Username": "Other", "Email": "o@example.com", "Uuid": "d0f2fa97-222a-4a09-a3d3-2b8d5a336310", "Package IDs": "5913134", "Package Names": "'Immortal Rank'"},
    ]
    mapping = CsvImportService.auto_map(headers, rows)
    assert mapping["rank"] == "Package Names" and mapping["uuid"] == "Uuid" and mapping["username"] == "Username"
    assert mapping["transaction_id"] == "Transaction ID" and mapping["purchase_date"] == "Date" and mapping["status"] == "Status"
    # without sample rows the name-based choice still works
    assert CsvImportService.auto_map(["username", "uuid", "rank"])["rank"] == "rank"


def test_rank_from_package_text():
    policy = default_policy()
    assert rank_from_package_text("'Galactic to Infinite'", policy) == ("INFINITE", "")
    assert rank_from_package_text("Immortal to Immortal+", policy) == ("IMMORTAL+", "")
    assert rank_from_package_text("'Immortal Rank', 'Fly Perk'", policy)[0] == "IMMORTAL"
    assert rank_from_package_text("'Scuffed Rank'", policy)[0] == "SCUFFED"
    assert rank_from_package_text("SOLAR", policy) == ("IMMORTAL", "")
    assert rank_from_package_text("'Mystery Crate'", policy)[0] is None
    assert rank_from_package_text("'Mystery Crate'", policy, {"mystery crate": "HERO"})[0] == "HERO"
    assert rank_from_package_text("", policy) == (None, "missing rank")


def test_tebex_export_preview_and_import(tmp_path):
    from rankrecovery.db.engine import Database, sqlite_url
    from rankrecovery.services.context import AppContext
    from rankrecovery.services.csvimport import CsvImportService
    from rankrecovery.services.playerview import PlayerViewService
    from rankrecovery.services.reconcile import ReconcileService

    db = Database(sqlite_url(tmp_path / "tebex.db"))
    db.run_migrations()
    ctx = AppContext(db)
    svc = CsvImportService(ctx)
    preview = svc.preview(TEBEX, filename="tebex.csv")
    assert preview.mapping["rank"] == "Packages" and preview.mapping["status"] == "Status" and preview.mapping["uuid"] == "Uuid" and preview.mapping["transaction_id"] == "Transaction ID"
    by_row = {r.row_number: r for r in preview.rows}
    assert by_row[2].status == "valid" and by_row[2].rank_canonical == "INFINITE"
    assert by_row[3].status == "invalid" and any("Declined" in e for e in by_row[3].errors)
    assert by_row[4].status == "valid" and by_row[4].rank_canonical == "IMMORTAL" and any("Fly Perk" in w for w in by_row[4].warnings)
    assert by_row[5].status == "invalid" and any("Mystery Crate" in e for e in by_row[5].errors)
    result = svc.commit(TEBEX, filename="tebex.csv", actor="daniel")
    assert result["status"] == "imported" and result["summary"]["valid"] == 2 and result["summary"]["invalid"] == 2
    recon = ReconcileService(ctx)
    for pid in recon.stale_player_ids():
        recon.recompute_player(pid)
    view = PlayerViewService(ctx)
    cj = view.lookup_by_name("CJ_ALT1")
    assert cj["summary"]["effective_rank"] == "INFINITE" and cj["player"]["uuid"] == "7285321a-ab57-4b70-a622-34ea17297e4d"
    assert view.lookup_by_name("Player4995")["summary"]["effective_rank"] == "IMMORTAL"
    # same file again is refused until the batch is retired
    again = svc.commit(TEBEX, filename="tebex.csv", actor="daniel")
    assert again["status"] == "already_imported"
    ctx.settings.set("csv.package_rank_mapping", {"mystery crate": "HERO"}, changed_by="t")
    retired = svc.retire_batch(result["batch_id"], actor="daniel", reason="re-import with crate mapping")
    assert retired["rows_retired"] == 2
    third = svc.commit(TEBEX, filename="tebex.csv", actor="daniel")
    assert third["status"] == "imported" and third["summary"]["valid"] == 3
    for pid in recon.stale_player_ids():
        recon.recompute_player(pid)
    assert view.lookup_by_name("Mani_Festive")["summary"]["effective_rank"] == "HERO"
    db.dispose()
