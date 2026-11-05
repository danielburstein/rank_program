"""Mode 1: players table with the full evidence inspector."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from rankrecovery.client.pages.base import Page
from rankrecovery.client.pages.images import ImageInspector
from rankrecovery.client.theme import mono_font, status_colour
from rankrecovery.client.widgets.data_table import DataTable
from rankrecovery.client.widgets.flag_list import FlagList
from rankrecovery.client.widgets.forms import ConfirmDialog, KeyValueForm, NoteDialog, error_box, info_box

PLAYER_COLUMNS = [
    ("display_name", "Player"),
    ("uuid", "UUID"),
    ("aliases", "Known names"),
    ("observed_ranks", "All old ranks"),
    ("highest_observed", "Highest observed"),
    ("highest_confirmed", "Highest confirmed"),
    ("effective_rank", "Effective"),
    ("provisional", "Provisional (n/m)"),
    ("staff_ranks", "Staff"),
    ("custom_prefixes", "Custom prefixes"),
    ("clean_rank_images", "Clean images"),
    ("distinct_submitters", "Submitters"),
    ("ai_rank_images", "AI images"),
    ("csv_records", "CSV records"),
    ("active_flags", "Active flags"),
    ("resolved_flags", "Resolved flags"),
    ("manual_override", "Manual override"),
    ("review_status", "Review"),
    ("identity_status", "Identity"),
    ("new_rank", "New rank"),
    ("export_included", "Export"),
    ("stale", "Stale"),
    ("player_id", "ID"),
]
OBS_COLUMNS = [("observation_id", "ID"), ("rank", "Rank"), ("raw_text", "Raw"), ("basis", "Basis"), ("source_type", "Source"), ("credibility", "Credibility"), ("confidence", "Conf"), ("review_status", "Review"), ("manually_verified", "Verified"), ("verified_by", "By"), ("is_active", "Active"), ("custom_prefix", "Custom"), ("image_filename", "Image"), ("submitter", "Submitter"), ("image_ai_status", "AI"), ("image_date_status", "Date"), ("image_id", "Image ID"), ("csv_row_id", "CSV row"), ("detection_id", "Detection"), ("created_by", "Created by")]
STAFF_COLUMNS = [("staff_observation_id", "ID"), ("staff", "Staff"), ("raw_text", "Raw"), ("has_yinyang", "☯"), ("review_status", "Review"), ("is_active", "Active"), ("image_filename", "Image"), ("submitter", "Submitter"), ("image_id", "Image ID")]
VERIFY_COLUMNS = [("rank", "Rank"), ("status", "Status"), ("progress", "Sources"), ("confirmed_by", "Confirmed by"), ("fallback_rank", "Fallback"), ("notes", "Notes")]
SOURCE_COLUMNS = [("observation_id", "Obs"), ("image_id", "Image"), ("submitter", "Submitter"), ("eligible", "Eligible"), ("counted", "Counts"), ("source_key", "Source key"), ("reasons", "Why excluded / notes")]
CSV_COLUMNS = [("csv_row_id", "ID"), ("batch_id", "Batch"), ("username", "Username"), ("uuid", "UUID"), ("rank_raw", "Rank raw"), ("rank", "Rank"), ("purchase_date", "Purchased"), ("transaction_id", "Transaction"), ("validation_status", "Validation"), ("is_active", "Active"), ("deactivated_reason", "Deactivated")]
LOOKUP_COLUMNS = [("attempt_id", "ID"), ("queried_name", "Name"), ("provider", "Provider"), ("attempt_no", "Try"), ("outcome", "Outcome"), ("failure_code", "Failure"), ("http_status", "HTTP"), ("proxy_id", "Proxy"), ("elapsed_ms", "ms"), ("resolved_uuid", "UUID"), ("resolved_name", "Current name"), ("error_message", "Error"), ("started_at", "At")]
RES_COLUMNS = [("id", "ID"), ("uuid", "UUID"), ("status", "Status"), ("source", "Source"), ("queried_name", "Queried"), ("current_name", "Current"), ("note", "Note"), ("is_active", "Active"), ("created_at", "At")]
MANUAL_COLUMNS = [("id", "ID"), ("kind", "Kind"), ("value", "Value"), ("reason", "Reason"), ("created_by", "By"), ("created_at", "At"), ("is_active", "Active")]
AUDIT_COLUMNS = [("id", "ID"), ("created_at", "At"), ("actor", "Actor"), ("action", "Action"), ("entity_type", "Entity"), ("entity_id", "Entity ID"), ("before", "Before"), ("after", "After"), ("note", "Note")]


def _obs_colour(row: dict) -> QColor | None:
    source = (row.get("source_type") or "").lower()
    if source == "trusted_csv":
        return status_colour("csv")
    if source in ("manual_override", "manual_screenshot") or row.get("manually_verified"):
        return status_colour("manual")
    if not row.get("is_active", True):
        return status_colour("resolved")
    if row.get("review_status") == "pending":
        return status_colour("pending")
    return None


def _player_colour(row: dict) -> QColor | None:
    if row.get("stale"):
        return status_colour("stale")
    if row.get("manual_override"):
        return status_colour("manual")
    if row.get("provisional"):
        return status_colour("provisional")
    if row.get("effective_rank"):
        return status_colour("confirmed")
    return None


class PlayerInspector(QWidget):
    def __init__(self, page: Page, parent: QWidget | None = None):
        super().__init__(parent)
        self.page = page
        self.detail: dict = {}
        self.player_id: int | None = None
        self.tabs = QTabWidget()
        # Summary
        summary = QWidget()
        s_layout = QVBoxLayout(summary)
        self.compact = QLabel("")
        self.compact.setWordWrap(True)
        self.compact.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.summary_form = KeyValueForm("Summary")
        self.reasoning = QPlainTextEdit()
        self.reasoning.setReadOnly(True)
        self.reasoning.setFont(mono_font())
        s_layout.addWidget(self.compact)
        s_layout.addWidget(self.summary_form)
        s_layout.addWidget(QLabel("Reasoning"))
        s_layout.addWidget(self.reasoning, 1)
        # Verification
        verification = QWidget()
        v_layout = QVBoxLayout(verification)
        self.candidates = DataTable(VERIFY_COLUMNS, colour_key="status")
        self.candidates.selection_changed.connect(self._candidate_selected)
        self.sources = DataTable(SOURCE_COLUMNS, colour_fn=lambda r: status_colour("confirmed") if r.get("counted") else (status_colour("error") if not r.get("eligible") else None))
        v_bar = QHBoxLayout()
        self.open_src_btn = QPushButton("Open image")
        self.verify_btn = QPushButton("Verify detection… (parsing only)")
        self.waive_btn = QPushButton("Waive submitter requirement… (audited override)")
        self.open_src_btn.clicked.connect(self._open_source_image)
        self.verify_btn.clicked.connect(self._verify_source)
        self.waive_btn.clicked.connect(self._waive)
        for b in (self.open_src_btn, self.verify_btn, self.waive_btn):
            v_bar.addWidget(b)
        v_bar.addStretch(1)
        self.verify_note = QLabel("A source is a submitter, not an image. Duplicates and AI-flagged images never count. Manual verification only confirms the parsing; it never adds a source.")
        self.verify_note.setObjectName("dim")
        self.verify_note.setWordWrap(True)
        v_layout.addWidget(self.verify_note)
        v_layout.addWidget(self.candidates, 1)
        v_layout.addWidget(QLabel("Evidence for the selected candidate"))
        v_layout.addWidget(self.sources, 2)
        v_layout.addLayout(v_bar)
        self.waivers = DataTable([("id", "ID"), ("rank", "Rank"), ("reason", "Reason"), ("created_by", "By"), ("is_active", "Active"), ("revoked_by", "Revoked by")])
        self.revoke_waiver_btn = QPushButton("Revoke selected waiver…")
        self.revoke_waiver_btn.clicked.connect(self._revoke_waiver)
        v_layout.addWidget(QLabel("Waivers"))
        v_layout.addWidget(self.waivers)
        v_layout.addWidget(self.revoke_waiver_btn)
        # Observations
        observations = QWidget()
        o_layout = QVBoxLayout(observations)
        legend = QLabel("Colour key: <span style='color:#2fb8b0'>trusted CSV</span> · <span style='color:#f08a3c'>manual / verified</span> · <span style='color:#e0a526'>pending review</span> · <span style='color:#9a9ca6'>inactive</span>")
        o_layout.addWidget(legend)
        self.observations = DataTable(OBS_COLUMNS, colour_fn=_obs_colour)
        o_bar = QHBoxLayout()
        self.obs_verify_btn = QPushButton("Mark verified…")
        self.obs_unverify_btn = QPushButton("Mark unverified…")
        self.obs_deactivate_btn = QPushButton("Deactivate…")
        self.obs_open_btn = QPushButton("Open image")
        self.obs_verify_btn.clicked.connect(lambda: self._verify_obs(True))
        self.obs_unverify_btn.clicked.connect(lambda: self._verify_obs(False))
        self.obs_deactivate_btn.clicked.connect(self._deactivate_obs)
        self.obs_open_btn.clicked.connect(self._open_obs_image)
        for b in (self.obs_verify_btn, self.obs_unverify_btn, self.obs_deactivate_btn, self.obs_open_btn):
            o_bar.addWidget(b)
        o_bar.addStretch(1)
        o_layout.addWidget(self.observations, 1)
        o_layout.addLayout(o_bar)
        self.staff = DataTable(STAFF_COLUMNS)
        self.flags = FlagList(on_resolve=self._resolve_flag, on_reopen=self._reopen_flag)
        self.csv_rows = DataTable(CSV_COLUMNS, colour_fn=lambda _r: status_colour("csv"))
        # Identity
        identity = QWidget()
        i_layout = QVBoxLayout(identity)
        self.identity_form = KeyValueForm("Identity")
        i_bar = QHBoxLayout()
        self.uuid_btn = QPushButton("Set manual UUID…")
        self.alias_btn = QPushButton("Add alias…")
        self.merge_btn = QPushButton("Merge another player into this one…")
        self.lookup_btn = QPushButton("Run UUID lookup")
        self.uuid_btn.clicked.connect(self._manual_uuid)
        self.alias_btn.clicked.connect(self._add_alias)
        self.merge_btn.clicked.connect(self._merge)
        self.lookup_btn.clicked.connect(self._lookup)
        for b in (self.uuid_btn, self.alias_btn, self.merge_btn, self.lookup_btn):
            i_bar.addWidget(b)
        i_bar.addStretch(1)
        self.resolutions = DataTable(RES_COLUMNS, colour_key="status")
        self.lookups = DataTable(LOOKUP_COLUMNS, colour_key="outcome")
        self.merges = DataTable([("id", "ID"), ("survivor", "Survivor"), ("merged", "Merged"), ("reason", "Reason"), ("created_by", "By"), ("created_at", "At"), ("undone_at", "Undone")])
        self.unmerge_btn = QPushButton("Undo selected merge…")
        self.unmerge_btn.clicked.connect(self._unmerge)
        i_layout.addWidget(self.identity_form)
        i_layout.addLayout(i_bar)
        i_layout.addWidget(QLabel("Identity resolutions"))
        i_layout.addWidget(self.resolutions, 1)
        i_layout.addWidget(QLabel("Lookup attempts"))
        i_layout.addWidget(self.lookups, 1)
        i_layout.addWidget(QLabel("Merges"))
        i_layout.addWidget(self.merges)
        i_layout.addWidget(self.unmerge_btn)
        # Manual actions
        manual = QWidget()
        m_layout = QVBoxLayout(manual)
        self.override_btn = QPushButton("Manual rank override…")
        self.staff_btn = QPushButton("Manual staff assertion…")
        self.review_btn = QPushButton("Set review status…")
        self.approve_btn = QPushButton("Conversion manual approval…")
        self.recompute_btn = QPushButton("Recompute this player")
        self.override_btn.clicked.connect(self._override)
        self.staff_btn.clicked.connect(self._manual_staff)
        self.review_btn.clicked.connect(self._review_status)
        self.approve_btn.clicked.connect(self._approve)
        self.recompute_btn.clicked.connect(self._recompute)
        for b in (self.override_btn, self.staff_btn, self.review_btn, self.approve_btn, self.recompute_btn):
            m_layout.addWidget(b)
        m_layout.addWidget(QLabel("Manual entries create superseding records with provenance; originals are never rewritten."))
        self.manual_table = DataTable(MANUAL_COLUMNS, colour_fn=lambda _r: status_colour("manual"))
        m_layout.addWidget(self.manual_table, 1)
        self.conversion_form = KeyValueForm("Conversion (Mode 2)")
        m_layout.addWidget(self.conversion_form)
        # Images
        images = QWidget()
        im_layout = QVBoxLayout(images)
        self.image_list = DataTable([("image_id", "ID"), ("filename", "File"), ("submitter", "Submitter"), ("ai_status", "AI"), ("date_status", "Date"), ("capture_at", "Capture")], colour_key="ai_status")
        self.image_list.selection_changed.connect(self._image_selected)
        self.image_inspector = ImageInspector(page)
        im_split = QSplitter(Qt.Orientation.Vertical)
        im_split.addWidget(self.image_list)
        im_split.addWidget(self.image_inspector)
        im_split.setSizes([160, 600])
        im_layout.addWidget(im_split)
        self.audit = DataTable(AUDIT_COLUMNS)
        self.exports = DataTable([("batch_id", "Export batch"), ("included", "Included"), ("skip_reason", "Skip reason"), ("command", "Command")])
        for widget, name in ((summary, "Summary"), (verification, "Verification"), (observations, "Observations"), (self.staff, "Staff"), (self.flags, "Flags"), (self.csv_rows, "Trusted CSV"), (identity, "Identity"), (manual, "Manual actions"), (images, "Images"), (self.exports, "Exports"), (self.audit, "Audit")):
            self.tabs.addTab(widget, name)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tabs)

    # ------------------------------------------------------------------ load
    def load(self, player_id: int) -> None:
        self.player_id = player_id
        self.page.call(self.page.client.player, player_id, on_result=self._fill)
        self.page.call(self.page.client.audit, "player", player_id, 200, on_result=self.audit.set_rows, on_error=lambda _m: None)

    def reload(self) -> None:
        if self.player_id is not None:
            self.load(self.player_id)

    def _fill(self, detail: dict) -> None:
        self.detail = detail
        p, s = detail["player"], detail["summary"]
        self.compact.setText(s.get("compact") or "")
        self.summary_form.set_values(
            {
                "Player": p.get("display_name"),
                "Known names": p.get("aliases"),
                "UUID": p.get("uuid"),
                "Identity": p.get("identity_status"),
                "Bedrock": p.get("is_bedrock"),
                "All old ranks": s.get("observed_ranks"),
                "Highest observed": s.get("highest_observed"),
                "Highest confirmed": s.get("highest_confirmed"),
                "Effective old rank": s.get("effective_rank"),
                "Provisional": [f"{x['rank']} {x['counted']}/{x['required']}" for x in s.get("provisional", [])],
                "Staff ranks": s.get("staff_ranks"),
                "Custom prefixes": s.get("custom_prefixes"),
                "Clean rank images": s.get("clean_rank_images"),
                "Distinct submitters": s.get("distinct_submitters"),
                "AI-flagged rank images": s.get("ai_rank_images"),
                "Trusted CSV records": s.get("csv_records"),
                "Manual override active": s.get("manual_override_active"),
                "Review status": p.get("review_status"),
                "Policy fingerprint": s.get("policy_fingerprint"),
                "Verification settings": ", ".join(f"{k}={v}" for k, v in (s.get("verification_settings") or {}).items()),
                "Computed": s.get("computed_at"),
                "Stale": s.get("stale"),
            }
        )
        self.reasoning.setPlainText("\n".join(s.get("reasoning") or []) or "-")
        self.candidates.set_rows(detail.get("verification", []))
        self.sources.set_rows([])
        self.waivers.set_rows(detail.get("waivers", []))
        self.observations.set_rows(detail.get("observations", []))
        self.staff.set_rows(detail.get("staff_observations", []))
        self.flags.set_flags(detail.get("flags", []))
        self.csv_rows.set_rows(detail.get("csv_rows", []))
        self.identity_form.set_values({"UUID": p.get("uuid"), "Status": p.get("identity_status"), "Source": p.get("identity_source"), "Current name": p.get("current_name"), "Merged members": p.get("merged_member_ids")})
        self.resolutions.set_rows(detail.get("identity_resolutions", []))
        self.lookups.set_rows(detail.get("lookups", []))
        self.merges.set_rows(detail.get("merges", []))
        self.manual_table.set_rows(detail.get("manual_assertions", []))
        conv = detail.get("conversion") or {}
        self.conversion_form.set_values({"Old rank": conv.get("old_rank"), "New rank": conv.get("new_rank"), "Included": conv.get("included"), "Reasons": conv.get("reasons"), "Manual approval": conv.get("manual_approval"), "Stale": conv.get("stale")})
        self.image_list.set_rows(detail.get("images", []))
        self.exports.set_rows(detail.get("exports", []))

    # ---------------------------------------------------------- verification
    def _candidate_selected(self, rows: list[dict]) -> None:
        self.sources.set_rows(rows[0].get("sources", []) if rows else [])

    def _open_source_image(self) -> None:
        row = self.sources.current_row()
        if row and row.get("image_id"):
            self.tabs.setCurrentIndex(8)
            self.image_inspector.load(int(row["image_id"]), highlight=self.detail.get("player", {}).get("display_name"))

    def _verify_source(self) -> None:
        row = self.sources.current_row()
        if not row or not row.get("observation_id"):
            error_box(self, "Select an evidence row with an observation.")
            return
        note = NoteDialog.ask(self, "Verify detection", "Confirm that this detection reads the prefix and username correctly. This marks the parsing as reliable; it does not add an independent source and does not prove the image is authentic.")
        if note:
            self.page.call(self.page.client.verify_observation, int(row["observation_id"]), True, note, on_result=lambda _r: self._recompute())

    def _waive(self) -> None:
        cand = self.candidates.current_row()
        if not cand:
            error_box(self, "Select a candidate rank first.")
            return
        dialog = ConfirmDialog("Waive submitter requirement", f"AUDITED OVERRIDE: waive the independent-submitter threshold for {cand['rank']} on this player. The waiver is recorded with your name and reason and shows as VERIFICATION_WAIVED.", parent=self, typed_confirmation="WAIVE")
        if dialog.exec():
            self.page.call(self.page.client.add_waiver, self.player_id, cand["rank"], dialog.value, on_result=lambda _r: self.reload())

    def _revoke_waiver(self) -> None:
        row = self.waivers.current_row()
        if not row:
            return
        note = NoteDialog.ask(self, "Revoke waiver", f"Revoke the waiver for {row.get('rank')}?")
        if note:
            self.page.call(self.page.client.revoke_waiver, int(row["id"]), note, on_result=lambda _r: self._recompute())

    # ------------------------------------------------------------ observations
    def _verify_obs(self, verified: bool) -> None:
        row = self.observations.current_row()
        if not row:
            return
        note = NoteDialog.ask(self, "Verify observation", "Reason for marking this observation as verified." if verified else "Reason for removing the verified mark.")
        if note:
            self.page.call(self.page.client.verify_observation, int(row["observation_id"]), verified, note, on_result=lambda _r: self._recompute())

    def _deactivate_obs(self) -> None:
        row = self.observations.current_row()
        if not row:
            return
        note = NoteDialog.ask(self, "Deactivate observation", "The observation stays stored but no longer counts. Reason:")
        if note:
            self.page.call(self.page.client.deactivate_observation, int(row["observation_id"]), note, on_result=lambda _r: self._recompute())

    def _open_obs_image(self) -> None:
        row = self.observations.current_row()
        if row and row.get("image_id"):
            self.tabs.setCurrentIndex(8)
            self.image_inspector.load(int(row["image_id"]), highlight=self.detail.get("player", {}).get("display_name"))

    def _image_selected(self, rows: list[dict]) -> None:
        if rows:
            self.image_inspector.load(int(rows[0]["image_id"]), highlight=self.detail.get("player", {}).get("display_name"))

    # ------------------------------------------------------------------ flags
    def _resolve_flag(self, flag_id: int, note: str) -> None:
        self.page.call(self.page.client.resolve_flag, flag_id, note, on_result=lambda _r: self._recompute())

    def _reopen_flag(self, flag_id: int, note: str) -> None:
        self.page.call(self.page.client.reopen_flag, flag_id, note, on_result=lambda _r: self._recompute())

    # --------------------------------------------------------------- identity
    def _manual_uuid(self) -> None:
        edit = QLineEdit(self.detail.get("player", {}).get("uuid") or "")
        dialog = NoteDialog("Manual UUID", "Enter the verified UUID (dashed or undashed). Leave empty to clear. Bedrock players use Floodgate UUIDs.", parent=self, extra_fields={"UUID": edit})
        if dialog.exec():
            self.page.call(self.page.client.set_manual_uuid, self.player_id, edit.text().strip() or None, dialog.value, on_result=lambda _r: self.reload())

    def _add_alias(self) -> None:
        edit = QLineEdit()
        dialog = NoteDialog("Add alias", "Add a historical username for this player.", parent=self, extra_fields={"Username": edit})
        if dialog.exec() and edit.text().strip():
            self.page.call(self.page.client.add_alias, self.player_id, edit.text().strip(), dialog.value, on_result=lambda _r: self.reload())

    def _merge(self) -> None:
        edit = QLineEdit()
        dialog = NoteDialog("Merge players", "Enter the player ID that should be merged INTO this player. You will see a preview before confirming.", parent=self, extra_fields={"Player ID to merge": edit})
        if not dialog.exec() or not edit.text().strip().isdigit():
            return
        other = int(edit.text())
        reason = dialog.value or ""

        def confirm(preview: dict) -> None:
            text = f"Survivor: {preview['survivor']}\n\nMerged: {preview['merged']}\n\nAfter: {preview['after']}\n\nUUID conflict: {preview['uuid_conflict']}"
            conf = ConfirmDialog("Confirm merge", text, parent=self, typed_confirmation="MERGE")
            conf.note.setPlainText(reason)
            if conf.exec():
                self.page.call(self.page.client.merge, self.player_id, other, conf.value, preview["uuid_conflict"], on_result=lambda _r: self.reload())

        self.page.call(self.page.client.merge_preview, self.player_id, other, on_result=confirm)

    def _unmerge(self) -> None:
        row = self.merges.current_row()
        if not row or row.get("undone_at"):
            return
        note = NoteDialog.ask(self, "Undo merge", f"Undo merge {row['id']} (player {row['merged']} becomes separate again)?")
        if note:
            self.page.call(self.page.client.unmerge, int(row["id"]), note, on_result=lambda _r: self.reload())

    def _lookup(self) -> None:
        self.page.call(self.page.client.lookup, player_ids=[self.player_id], force=True, on_result=lambda r: self.page.watch_job(int(r["job_id"]), label="lookup", on_finished=lambda _j: self.reload()))

    # ----------------------------------------------------------------- manual
    def _override(self) -> None:
        combo = QComboBox()
        combo.addItems([r["name"] for r in self.page.state.policy.get("ranks", []) if r.get("enabled", True)])
        dialog = ConfirmDialog("Manual rank override", "Record an explicit manual old-rank assertion. It is shown as MANUAL_OVERRIDE and audited. The highest confirmed rank still wins.", parent=self, extra_fields={"Rank": combo})
        if dialog.exec():
            self.page.call(self.page.client.override_rank, self.player_id, combo.currentText(), dialog.value, on_result=lambda _r: self.reload())

    def _manual_staff(self) -> None:
        combo = QComboBox()
        combo.addItems([s["name"] for s in self.page.state.policy.get("staff", [])])
        dialog = NoteDialog("Manual staff assertion", "Record a staff designation for this player.", parent=self, extra_fields={"Staff": combo})
        if dialog.exec():
            self.page.call(self.page.client.manual_staff, self.player_id, combo.currentText(), dialog.value, on_result=lambda _r: self.reload())

    def _review_status(self) -> None:
        combo = QComboBox()
        combo.addItems(["pending", "reviewed", "clear", "needs_attention"])
        dialog = NoteDialog("Review status", "Set the human review status for this player.", parent=self, extra_fields={"Status": combo})
        if dialog.exec():
            self.page.call(self.page.client.set_review_status, self.player_id, combo.currentText(), dialog.value, on_result=lambda _r: self.reload())

    def _approve(self) -> None:
        box = QCheckBox("Approve for conversion despite blocking flags (verification failures are never bypassed)")
        dialog = NoteDialog("Conversion approval", "Manual approval lets flagged players through the Mode 2 flag filter. It does not override a failed verification.", parent=self, extra_fields={"": box})
        if dialog.exec():
            self.page.call(self.page.client.conversion_approval, self.player_id, box.isChecked(), dialog.value, on_result=lambda _r: self.reload())

    def _recompute(self) -> None:
        if self.player_id is not None:
            self.page.call(self.page.client.recompute_player, self.player_id, on_result=lambda _r: self.reload())


class PlayersPage(Page):
    title = "Mode 1 · Players and old ranks"

    def __init__(self, state, parent=None):  # noqa: ANN001
        super().__init__(state, parent)
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("search name, alias or UUID")
        self.search.returnPressed.connect(self.refresh)
        self.only_stale = QCheckBox("only stale")
        self.only_provisional = QCheckBox("only provisional")
        self.flag_filter = QComboBox()
        self.flag_filter.addItem("any flag")
        self.sort = QComboBox()
        self.sort.addItems(["display_name", "effective_rank", "highest_observed", "submitters", "images", "identity_status", "updated"])
        self.desc = QCheckBox("desc")
        self.refresh_btn = QPushButton("Search")
        self.refresh_btn.setObjectName("primary")
        self.refresh_btn.clicked.connect(self.refresh)
        for w in (self.search, self.only_stale, self.only_provisional, QLabel("flag"), self.flag_filter, QLabel("sort"), self.sort, self.desc, self.refresh_btn):
            bar.addWidget(w)
        self.root.addLayout(bar)
        self.table = DataTable(PLAYER_COLUMNS, paginated=True, colour_fn=_player_colour)
        self.table.page_changed.connect(lambda _l, _o: self.refresh())
        self.table.selection_changed.connect(self._selected)
        self.table.add_row_action("Open player", lambda r: self.open_player(int(r["player_id"])))
        self.inspector = PlayerInspector(self)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.table)
        split.addWidget(self.inspector)
        split.setSizes([300, 650])
        self.root.addWidget(split, 1)
        state.open_player.connect(self.open_player)

    def refresh(self) -> None:
        if self.flag_filter.count() == 1 and self.state.flag_catalogue:
            self.flag_filter.addItems([f["code"] for f in self.state.flag_catalogue])
        params = {
            "search": self.search.text().strip(),
            "limit": self.table.limit_box.value(),
            "offset": self.table.offset,
            "sort": self.sort.currentText(),
            "descending": self.desc.isChecked(),
            "only_stale": self.only_stale.isChecked(),
            "only_provisional": self.only_provisional.isChecked(),
            "flag": None if self.flag_filter.currentIndex() == 0 else self.flag_filter.currentText(),
        }
        self.call(self.client.players, on_result=lambda r: self.table.set_rows(r.get("rows", []), r.get("total")), **params)

    def _selected(self, rows: list[dict]) -> None:
        if rows:
            self.inspector.load(int(rows[0]["player_id"]))

    def open_player(self, player_id: int) -> None:
        self._loaded = True
        self.inspector.load(player_id)
        self.status(f"opened player {player_id}")

    def info(self, text: str) -> None:
        info_box(self, text)
