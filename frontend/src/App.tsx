import { useCallback, useEffect, useMemo, useState } from "react";
import { dashboardApi } from "./api";
import type {
  AlignedCuePair,
  AssetDetailResponse,
  AssetState,
  AuditEventItem,
  ChannelStatusResponse,
  DashboardOperatorResponse,
  DiscoveredVideoSummary,
  LanguageAssetSummary,
  PolicySettingsResponse,
  PublishMode,
  VideoLocalizationResponse,
} from "./types";

function formatSrtTime(ms: number): string {
  const totalSec = Math.floor(ms / 1000);
  const millis = ms % 1000;
  const hrs = Math.floor(totalSec / 3600);
  const mins = Math.floor((totalSec % 3600) / 60);
  const secs = totalSec % 60;
  return `${String(hrs).padStart(2, "0")}:${String(mins).padStart(
    2,
    "0"
  )}:${String(secs).padStart(2, "0")},${String(millis).padStart(3, "0")}`;
}

function buildSrtFromCues(cues: AlignedCuePair[]): string {
  return (
    cues
      .map(
        (c, idx) =>
          `${idx + 1}\n${formatSrtTime(c.start_ms)} --> ${formatSrtTime(
            c.end_ms
          )}\n${c.translated_text.trim()}`
      )
      .join("\n\n") + "\n"
  );
}

type LocalizeScope = "all" | "metadata" | "subtitles";

function stateBadgeClass(state: AssetState | string): string {
  if (state === "ready_to_publish" || state === "published") {
    return "badge badge-success";
  }
  if (state === "awaiting_approval" || state === "awaiting_source") {
    return "badge badge-warning";
  }
  if (
    state === "needs_attention" ||
    state === "failed" ||
    state === "uncertain"
  ) {
    return "badge badge-danger";
  }
  return "badge";
}

export function App() {
  const [operator, setOperator] = useState<DashboardOperatorResponse | null>(
    null
  );
  const [channelStatus, setChannelStatus] =
    useState<ChannelStatusResponse | null>(null);
  const [videos, setVideos] = useState<DiscoveredVideoSummary[]>([]);
  const [selectedVideoId, setSelectedVideoId] = useState<string | null>(null);
  const [importVideoInput, setImportVideoInput] = useState("");
  const [videoSearchQuery, setVideoSearchQuery] = useState("");
  const [localization, setLocalization] =
    useState<VideoLocalizationResponse | null>(null);
  const [selectedAsset, setSelectedAsset] =
    useState<AssetDetailResponse | null>(null);
  const [policySettings, setPolicySettings] =
    useState<PolicySettingsResponse | null>(null);
  const [auditEvents, setAuditEvents] = useState<AuditEventItem[]>([]);
  const [activeTab, setActiveTab] = useState<"review" | "settings" | "audit">(
    "review"
  );
  const [busyMessage, setBusyMessage] = useState<string | null>(null);
  const [errorBanner, setErrorBanner] = useState<string | null>(null);
  const [noticeBanner, setNoticeBanner] = useState<string | null>(null);

  // Editor state
  const [editTitle, setEditTitle] = useState("");
  const [editDescription, setEditDescription] = useState("");
  const [editCues, setEditCues] = useState<AlignedCuePair[]>([]);
  const [rawSrtMode, setRawSrtMode] = useState(false);
  const [editRawSrt, setEditRawSrt] = useState("");
  const [approvalNotes, setApprovalNotes] = useState("");
  const [overrideReview, setOverrideReview] = useState(false);
  const [rejectReason, setRejectReason] = useState("");

  const loadInitialData = useCallback(async () => {
    try {
      setErrorBanner(null);
      const op = await dashboardApi.getOperator();
      setOperator(op);
      if (!op.authenticated || !op.organization_id) {
        return;
      }
      const [chan, vids, pol, audits] = await Promise.all([
        dashboardApi.getChannelStatus(),
        dashboardApi.listVideos(),
        dashboardApi.getPolicySettings(),
        dashboardApi.listAuditEvents(50),
      ]);
      setChannelStatus(chan);
      setVideos(vids);
      setPolicySettings(pol);
      setAuditEvents(audits);
      if (
        vids.length > 0 &&
        (!selectedVideoId || !vids.some((video) => video.youtube_video_id === selectedVideoId))
      ) {
        const preferred =
          vids.find((v) => v.privacy_status === "private") ?? vids[0];
        setSelectedVideoId(preferred.youtube_video_id);
      }
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    }
  }, [selectedVideoId]);

  useEffect(() => {
    void loadInitialData();
  }, [loadInitialData]);

  const loadVideoAssets = useCallback(async (videoId: string) => {
    try {
      const res = await dashboardApi.getVideoAssets(videoId);
      setLocalization(res);
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    if (selectedVideoId && operator?.authenticated) {
      void loadVideoAssets(selectedVideoId);
      setSelectedAsset(null);
    }
  }, [selectedVideoId, operator?.authenticated, loadVideoAssets]);

  const openAssetEditor = async (assetId: number) => {
    try {
      setErrorBanner(null);
      setNoticeBanner(null);
      const detail = await dashboardApi.getAssetDetail(assetId);
      setSelectedAsset(detail);
      setEditTitle(detail.translated_title ?? "");
      setEditDescription(detail.translated_description ?? "");
      setEditCues(detail.aligned_cues.map((c) => ({ ...c })));
      setEditRawSrt(detail.caption_content ?? "");
      setApprovalNotes("");
      setOverrideReview(!detail.review_passed || detail.source_ambiguous);
      setRejectReason("");
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    }
  };

  const handleDiscoverUploads = async (maxResults = 50) => {
    try {
      setBusyMessage(`Discovering up to ${maxResults} channel uploads...`);
      setErrorBanner(null);
      const res = await dashboardApi.discoverVideos(maxResults);
      const allVids = await dashboardApi.listVideos();
      setVideos(allVids);
      const chan = await dashboardApi.getChannelStatus();
      setChannelStatus(chan);
      setNoticeBanner(
        `Synced ${res.discovered_count} channel uploads (${allVids.length} total in queue).`
      );
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyMessage(null);
    }
  };

  const handleImportVideo = async (e: React.FormEvent) => {
    e.preventDefault();
    const raw = importVideoInput.trim();
    if (!raw) return;
    try {
      setBusyMessage(`Importing video '${raw}' from YouTube...`);
      setErrorBanner(null);
      const imported = await dashboardApi.importVideo(raw);
      const [allVids, chan] = await Promise.all([
        dashboardApi.listVideos(),
        dashboardApi.getChannelStatus(),
      ]);
      setVideos(allVids);
      setChannelStatus(chan);
      setSelectedVideoId(imported.youtube_video_id);
      setImportVideoInput("");
      setNoticeBanner(
        `Imported video '${imported.source_title}' (${imported.youtube_video_id}).`
      );
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyMessage(null);
    }
  };

  const handleInspectCaptions = async () => {
    if (!selectedVideoId) return;
    try {
      setBusyMessage("Inspecting Turkish source caption tracks on YouTube...");
      setErrorBanner(null);
      await dashboardApi.inspectVideoCaptions(selectedVideoId);
      await loadVideoAssets(selectedVideoId);
      setNoticeBanner("Source caption tracks inspected and recorded.");
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyMessage(null);
    }
  };

  const handleLocalizeVideo = async (
    langSubset?: string[],
    scope: LocalizeScope = "all"
  ) => {
    if (!selectedVideoId) return;
    try {
      const scopeLabel =
        scope === "metadata"
          ? "Title & Description only (Google Translate → LLM)"
          : scope === "subtitles"
            ? "Subtitles only (YouTube → Google Translate → LLM)"
            : "Title, Description & Subtitles";
      setBusyMessage(
        langSubset
          ? `Running localization (${scopeLabel}) for ${langSubset.join(", ")}...`
          : `Running localization (${scopeLabel}) across enabled languages...`
      );
      setErrorBanner(null);
      const res = await dashboardApi.localizeVideo(selectedVideoId, {
        languages: langSubset,
        include_metadata: scope !== "subtitles",
        include_captions: scope !== "metadata",
      });
      setLocalization(res);
      setNoticeBanner(
        `Localization (${scopeLabel}) complete (${res.quota_units_used_this_run} YouTube quota units used).`
      );
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyMessage(null);
    }
  };

  const handleSaveAssetEdits = async () => {
    if (!selectedAsset) return;
    try {
      setBusyMessage("Saving edits and running deterministic validation...");
      setErrorBanner(null);
      const payload =
        selectedAsset.kind === "metadata"
          ? {
              translated_title: editTitle,
              translated_description: editDescription,
            }
          : {
              caption_content: rawSrtMode
                ? editRawSrt
                : buildSrtFromCues(editCues),
            };
      const updated = await dashboardApi.editAsset(selectedAsset.id, payload);
      setSelectedAsset(updated);
      setEditCues(updated.aligned_cues.map((c) => ({ ...c })));
      setEditRawSrt(updated.caption_content ?? "");
      if (selectedVideoId) {
        await loadVideoAssets(selectedVideoId);
      }
      const audits = await dashboardApi.listAuditEvents(50);
      setAuditEvents(audits);
      setNoticeBanner(
        "Edits saved and validated. Any prior approval was automatically invalidated because the content hash changed."
      );
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyMessage(null);
    }
  };

  const handleApproveAsset = async () => {
    if (!selectedAsset || !selectedAsset.content_hash) return;
    try {
      setBusyMessage("Recording hash-bound approval...");
      setErrorBanner(null);
      const updated = await dashboardApi.approveAsset(selectedAsset.id, {
        expected_content_hash: selectedAsset.content_hash,
        notes: approvalNotes || undefined,
        override_review_issues: overrideReview,
      });
      setSelectedAsset(updated);
      if (selectedVideoId) {
        await loadVideoAssets(selectedVideoId);
      }
      const audits = await dashboardApi.listAuditEvents(50);
      setAuditEvents(audits);
      setNoticeBanner(
        `Approved asset #${updated.id} bound to hash ${updated.content_hash?.slice(
          0,
          20
        )}...`
      );
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyMessage(null);
    }
  };

  const handleRejectAsset = async () => {
    if (!selectedAsset) return;
    const reason =
      rejectReason.trim() || "Needs manual revision before publishing";
    try {
      setBusyMessage("Rejecting asset...");
      setErrorBanner(null);
      const updated = await dashboardApi.rejectAsset(selectedAsset.id, reason);
      setSelectedAsset(updated);
      if (selectedVideoId) {
        await loadVideoAssets(selectedVideoId);
      }
      const audits = await dashboardApi.listAuditEvents(50);
      setAuditEvents(audits);
      setNoticeBanner(`Asset #${updated.id} marked Needs Attention.`);
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyMessage(null);
    }
  };

  const handlePublishAsset = async () => {
    if (!selectedAsset) return;
    try {
      setBusyMessage(
        `Publishing ${selectedAsset.language.toUpperCase()} ${selectedAsset.kind} to YouTube and verifying read-back...`
      );
      setErrorBanner(null);
      const res = await dashboardApi.publishAsset(selectedAsset.id, {
        expected_content_hash: selectedAsset.content_hash ?? undefined,
      });
      setSelectedAsset(res.asset);
      if (selectedVideoId) {
        await loadVideoAssets(selectedVideoId);
      }
      const [chan, audits] = await Promise.all([
        dashboardApi.getChannelStatus(),
        dashboardApi.listAuditEvents(50),
      ]);
      setChannelStatus(chan);
      setAuditEvents(audits);
      if (res.asset.state === "published") {
        setNoticeBanner(
          res.idempotent_reused
            ? `Idempotent publish verified (already published for hash ${res.asset.content_hash?.slice(
                0,
                16
              )}...).`
            : `Published & verified on YouTube (${res.quota_units_used} quota units used).`
        );
      } else if (res.asset.state === "skipped") {
        setNoticeBanner(
          `Publish skipped: ${res.asset.last_error ?? "Target already exists on YouTube."}`
        );
      } else {
        setErrorBanner(
          `Publish ended in state '${res.asset.state}': ${res.asset.last_error ?? ""}`
        );
      }
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyMessage(null);
    }
  };

  const handleReconcileAsset = async () => {
    if (!selectedAsset) return;
    try {
      setBusyMessage(
        "Reconciling uncertain publish outcome against live YouTube state..."
      );
      setErrorBanner(null);
      const res = await dashboardApi.reconcileAsset(
        selectedAsset.id,
        "Manual reconciliation from dashboard"
      );
      setSelectedAsset(res.asset);
      if (selectedVideoId) {
        await loadVideoAssets(selectedVideoId);
      }
      const [chan, audits] = await Promise.all([
        dashboardApi.getChannelStatus(),
        dashboardApi.listAuditEvents(50),
      ]);
      setChannelStatus(chan);
      setAuditEvents(audits);
      setNoticeBanner(
        `Reconciliation complete: asset state is now '${res.asset.state}'.`
      );
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyMessage(null);
    }
  };

  const handlePublishReadyForVideo = async (force = false) => {
    if (!selectedVideoId) return;
    try {
      setBusyMessage(
        force
          ? "Force-publishing all localized assets (skipping manual reconciliation & auto-approving validated assets)..."
          : "Publishing all ready_to_publish assets for this video and verifying read-back..."
      );
      setErrorBanner(null);
      const res = await dashboardApi.publishReadyAssetsForVideo(
        selectedVideoId,
        { force }
      );
      await loadVideoAssets(selectedVideoId);
      if (selectedAsset) {
        const refreshed = await dashboardApi.getAssetDetail(selectedAsset.id);
        setSelectedAsset(refreshed);
      }
      const [chan, audits] = await Promise.all([
        dashboardApi.getChannelStatus(),
        dashboardApi.listAuditEvents(50),
      ]);
      setChannelStatus(chan);
      setAuditEvents(audits);
      setNoticeBanner(
        `${force ? "Force publish" : "Batch publish"} finished: ${res.published_asset_ids.length} published, ${res.skipped_asset_ids.length} skipped, ${res.uncertain_asset_ids.length} uncertain, ${res.failed_asset_ids.length} failed (${res.quota_units_used} quota units).`
      );
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyMessage(null);
    }
  };

  const handleLanguagePolicyChange = async (
    code: string,
    updates: { mode?: PublishMode; enabled?: boolean }
  ) => {
    try {
      setErrorBanner(null);
      const updated = await dashboardApi.updateLanguagePolicy(code, updates);
      setPolicySettings(updated);
      const audits = await dashboardApi.listAuditEvents(50);
      setAuditEvents(audits);
      setNoticeBanner(`Updated policy for '${code}' (recorded in audit log).`);
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    }
  };

  const handleGlobalAutoPublishToggle = async (nextVal: boolean) => {
    try {
      setErrorBanner(null);
      const updated = await dashboardApi.updateGlobalPolicy(
        nextVal,
        nextVal
          ? "Owner enabled global auto_publish_enabled from dashboard"
          : "Owner disabled global auto_publish_enabled from dashboard"
      );
      setPolicySettings(updated);
      const audits = await dashboardApi.listAuditEvents(50);
      setAuditEvents(audits);
      setNoticeBanner(
        `Global auto_publish_enabled set to ${String(nextVal)} (recorded in audit log).`
      );
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    }
  };

  const groupedByLanguage = useMemo(() => {
    const map = new Map<
      string,
      { metadata?: LanguageAssetSummary; caption?: LanguageAssetSummary }
    >();
    for (const lang of policySettings?.languages ?? []) {
      map.set(lang.code, {});
    }
    for (const asset of localization?.assets ?? []) {
      const entry = map.get(asset.language) ?? {};
      if (asset.kind === "metadata") {
        entry.metadata = asset;
      } else {
        entry.caption = asset;
      }
      map.set(asset.language, entry);
    }
    return map;
  }, [localization, policySettings]);

  const descriptionBytes = useMemo(
    () => new TextEncoder().encode(editDescription).length,
    [editDescription]
  );

  if (operator && !operator.authenticated) {
    return (
      <div className="workspace" style={{ maxWidth: 440, margin: "4rem auto" }}>
        <div className="editor-panel">
          <h2 style={{ marginTop: 0 }}>Dashboard Sign-In</h2>
          <p className="brand-subtitle">
            Sign in with your verified Google account to access your workspace.
          </p>
          {errorBanner && (
            <div className="alert-banner badge-danger">{errorBanner}</div>
          )}
          <button
            className="btn btn-primary"
            onClick={() => dashboardApi.startLogin()}
          >
            Continue with Google
          </button>
        </div>
      </div>
    );
  }

  if (operator?.authenticated && !operator.organization_id) {
    return (
      <div className="workspace" style={{ maxWidth: 560, margin: "4rem auto" }}>
        <div className="editor-panel">
          <h2 style={{ marginTop: 0 }}>Choose a workspace</h2>
          <p className="brand-subtitle">
            Signed in as {operator.email}. Select a workspace you belong to.
          </p>
          {operator.organizations.map((organization) => (
            <button
              className="btn btn-primary"
              key={organization.id}
              style={{ marginRight: "0.5rem", marginBottom: "0.5rem" }}
              onClick={async () => {
                try {
                  const identity = await dashboardApi.activateOrganization(organization.id);
                  setOperator(identity);
                  await loadInitialData();
                } catch (err) {
                  setErrorBanner(err instanceof Error ? err.message : String(err));
                }
              }}
            >
              {organization.name} ({organization.role})
            </button>
          ))}
          {errorBanner && <div className="alert-banner badge-danger">{errorBanner}</div>}
        </div>
      </div>
    );
  }

  return (
    <div className="dashboard-shell">
      <header className="top-header">
        <div className="brand-block">
          <div>
            <h1 className="brand-title">
              YouTube Multilingual Publishing Agent
            </h1>
            <div className="brand-subtitle">
              Channel:{" "}
              <strong>
                {channelStatus?.channel_title ?? "Not connected"}
              </strong>{" "}
              ({channelStatus?.custom_url ?? ""}{" "}
              <code>{channelStatus?.youtube_channel_id ?? ""}</code>)
            </div>
          </div>
        </div>

        <div className="header-pills">
          <span
            className={
              channelStatus?.connected
                ? "badge badge-success"
                : "badge badge-danger"
            }
          >
            OAuth: {channelStatus?.status ?? "disconnected"}
          </span>
          <span className="badge">
            Quota Today: {channelStatus?.today_quota_units_used ?? 0} /{" "}
            {channelStatus?.daily_quota_limit_estimate ?? 10000}
          </span>
          <span
            className={
              policySettings?.youtube_publishing_enabled
                ? "badge badge-warning"
                : "badge"
            }
          >
            Kill Switch:{" "}
            {policySettings?.youtube_publishing_enabled
              ? "Writes Enabled"
              : "Writes Blocked (Safe Mode)"}
          </span>
          <span
            className={
              policySettings?.global_auto_publish_enabled
                ? "badge badge-success"
                : "badge"
            }
          >
            Auto-Publish:{" "}
            {policySettings?.global_auto_publish_enabled ? "ON" : "OFF"}
          </span>
          <span className="badge">
            {operator?.organization_name} · {operator?.email} ({operator?.role})
          </span>
          {operator && operator.organizations.length > 1 && (
            <select
              aria-label="Active workspace"
              value={operator.organization_id ?? ""}
              onChange={async (event) => {
                try {
                  const identity = await dashboardApi.activateOrganization(event.target.value);
                  setOperator(identity);
                  setSelectedVideoId(null);
                  setLocalization(null);
                  setSelectedAsset(null);
                  await loadInitialData();
                } catch (err) {
                  setErrorBanner(err instanceof Error ? err.message : String(err));
                }
              }}
            >
              {operator.organizations.map((organization) => (
                <option key={organization.id} value={organization.id}>
                  {organization.name}
                </option>
              ))}
            </select>
          )}
          <button
            className="btn"
            onClick={() => void dashboardApi.logoutOperator()}
          >
            Sign Out
          </button>
        </div>
      </header>

      <div className="main-layout">
        <aside className="video-sidebar">
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              gap: "0.35rem",
              flexWrap: "wrap",
            }}
          >
            <strong>Channel Videos ({videos.length})</strong>
            <div style={{ display: "flex", gap: "0.35rem" }}>
              <button
                className="btn btn-primary"
                onClick={() => void handleDiscoverUploads(50)}
                disabled={Boolean(busyMessage)}
                title="Sync the 50 most recent uploads from your channel (costs 2 quota units)"
              >
                Sync (50)
              </button>
              <button
                className="btn"
                onClick={() => void handleDiscoverUploads(500)}
                disabled={Boolean(busyMessage)}
                title="Paginate through up to 500 channel uploads to discover older videos"
              >
                Sync All
              </button>
            </div>
          </div>

          <form
            onSubmit={(e) => void handleImportVideo(e)}
            style={{ display: "flex", gap: "0.35rem", marginTop: "0.5rem" }}
          >
            <input
              type="text"
              placeholder="Paste YouTube URL or Video ID..."
              value={importVideoInput}
              onChange={(e) => setImportVideoInput(e.target.value)}
              style={{ flex: 1, minWidth: 0, fontSize: "0.8rem", padding: "0.35rem 0.5rem" }}
            />
            <button
              type="submit"
              className="btn btn-primary"
              disabled={Boolean(busyMessage) || !importVideoInput.trim()}
              title="Import any older video from your channel by pasting its YouTube URL or 11-char ID (1 quota unit)"
            >
              Add
            </button>
          </form>

          {videos.length > 5 && (
            <input
              type="text"
              placeholder="Filter loaded videos by title or ID..."
              value={videoSearchQuery}
              onChange={(e) => setVideoSearchQuery(e.target.value)}
              style={{
                width: "100%",
                boxSizing: "border-box",
                fontSize: "0.8rem",
                padding: "0.35rem 0.5rem",
                marginTop: "0.35rem",
              }}
            />
          )}

          {videos
            .filter((v) => {
              const q = videoSearchQuery.trim().toLowerCase();
              if (!q) return true;
              return (
                v.source_title.toLowerCase().includes(q) ||
                v.youtube_video_id.toLowerCase().includes(q)
              );
            })
            .map((v) => (
            <button
              key={v.youtube_video_id}
              className={`video-card ${
                selectedVideoId === v.youtube_video_id ? "active" : ""
              }`}
              onClick={() => setSelectedVideoId(v.youtube_video_id)}
            >
              <div className="video-card-title">{v.source_title}</div>
              <div className="video-card-meta">
                <span
                  className={
                    v.privacy_status === "private"
                      ? "badge badge-warning"
                      : "badge"
                  }
                >
                  {v.privacy_status}
                </span>
                <span className={stateBadgeClass(v.workflow_state)}>
                  {v.workflow_state}
                </span>
                <span>ID: {v.youtube_video_id}</span>
              </div>
            </button>
          ))}
        </aside>

        <main className="workspace">
          <div className="tab-bar">
            <button
              className={`btn ${activeTab === "review" ? "btn-primary" : ""}`}
              onClick={() => setActiveTab("review")}
            >
              1. Video Localization & Review
            </button>
            <button
              className={`btn ${activeTab === "settings" ? "btn-primary" : ""}`}
              onClick={() => setActiveTab("settings")}
            >
              2. Language & Publishing Policy
            </button>
            <button
              className={`btn ${activeTab === "audit" ? "btn-primary" : ""}`}
              onClick={() => setActiveTab("audit")}
            >
              3. Audit Log ({auditEvents.length})
            </button>
          </div>

          {busyMessage && (
            <div className="alert-banner badge-warning">{busyMessage}</div>
          )}
          {errorBanner && (
            <div className="alert-banner badge-danger">{errorBanner}</div>
          )}
          {noticeBanner && (
            <div className="alert-banner badge-success">{noticeBanner}</div>
          )}

          {activeTab === "review" && (
            <>
              {localization ? (
                <div
                  style={{
                    display: "flex",
                    flexWrap: "wrap",
                    justifyContent: "space-between",
                    alignItems: "center",
                    gap: "0.75rem",
                  }}
                >
                  <div>
                    <h2 style={{ margin: 0 }}>{localization.source_title}</h2>
                    <div className="brand-subtitle">
                      Video ID: <code>{localization.youtube_video_id}</code> ·
                      Source Turkish Caption:{" "}
                      <strong>
                        {localization.source_caption_ready
                          ? `Ready (${localization.selected_source_track_kind})`
                          : "Not inspected / Awaiting"}
                      </strong>
                    </div>
                  </div>
                  <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
                    <button
                      className="btn"
                      onClick={() => void handleInspectCaptions()}
                      disabled={Boolean(busyMessage)}
                    >
                      Inspect Source Captions
                    </button>
                    <button
                      className="btn btn-primary"
                      onClick={() =>
                        void handleLocalizeVideo(undefined, "metadata")
                      }
                      disabled={Boolean(busyMessage)}
                      title="Translate only Title & Description: Google Translate first, LLM fallback (0 YouTube API quota units)"
                    >
                      Localize Only Title & Description
                    </button>
                    <button
                      className="btn btn-primary"
                      onClick={() =>
                        void handleLocalizeVideo(undefined, "subtitles")
                      }
                      disabled={Boolean(busyMessage)}
                      title="Translate only Subtitles: YouTube native first, then Google Translate, then LLM when quotas are reached"
                    >
                      Localize Only Subtitles
                    </button>
                    <button
                      className="btn btn-primary"
                      onClick={() => void handleLocalizeVideo(undefined, "all")}
                      disabled={Boolean(busyMessage)}
                      title="Translate Title, Description & Subtitles for all enabled languages"
                    >
                      Localize All (Title, Desc & Subtitles)
                    </button>
                    <button
                      className="btn btn-success"
                      onClick={() => void handlePublishReadyForVideo(false)}
                      disabled={
                        Boolean(busyMessage) ||
                        !policySettings?.youtube_publishing_enabled
                      }
                      title={
                        policySettings?.youtube_publishing_enabled
                          ? "Publish all ready_to_publish assets to YouTube"
                          : "YOUTUBE_PUBLISHING_ENABLED is false in .env"
                      }
                    >
                      Publish Ready Assets
                    </button>
                    <button
                      className="btn btn-danger"
                      onClick={() => void handlePublishReadyForVideo(true)}
                      disabled={
                        Boolean(busyMessage) ||
                        !policySettings?.youtube_publishing_enabled
                      }
                      title={
                        policySettings?.youtube_publishing_enabled
                          ? "Skip manual reconciliation, auto-approve all validated localized assets, and publish all immediately"
                          : "YOUTUBE_PUBLISHING_ENABLED is false in .env"
                      }
                    >
                      Force Publish All
                    </button>
                  </div>
                </div>
              ) : (
                <p>Select a video from the left sidebar to begin review.</p>
              )}

              <div className="lang-grid">
                {(policySettings?.languages ?? []).map((lang) => {
                  const pair = groupedByLanguage.get(lang.code);
                  const meta = pair?.metadata;
                  const cap = pair?.caption;
                  return (
                    <div key={lang.code} className="lang-card">
                      <div
                        style={{
                          display: "flex",
                          justifyContent: "space-between",
                          alignItems: "center",
                        }}
                      >
                        <div>
                          <strong>
                            {lang.name} ({lang.native_name})
                          </strong>{" "}
                          <code>{lang.code}</code>
                          {lang.rtl && (
                            <span
                              className="badge"
                              style={{ marginLeft: "0.35rem" }}
                            >
                              RTL
                            </span>
                          )}
                        </div>
                        <div style={{ display: "flex", gap: "0.35rem" }}>
                          <button
                            className="btn"
                            style={{ padding: "0.25rem 0.55rem", fontSize: "0.75rem" }}
                            onClick={() =>
                              void handleLocalizeVideo([lang.code], "metadata")
                            }
                            disabled={Boolean(busyMessage)}
                            title="Localize only Title & Description for this language (0 YouTube quota)"
                          >
                            Title & Desc Only
                          </button>
                          <button
                            className="btn"
                            style={{ padding: "0.25rem 0.55rem", fontSize: "0.75rem" }}
                            onClick={() =>
                              void handleLocalizeVideo([lang.code], "subtitles")
                            }
                            disabled={Boolean(busyMessage)}
                            title="Localize only Subtitles for this language (YouTube → Google Translate → LLM)"
                          >
                            Subs Only
                          </button>
                          <button
                            className="btn"
                            style={{ padding: "0.25rem 0.55rem", fontSize: "0.75rem" }}
                            onClick={() => void handleLocalizeVideo([lang.code], "all")}
                            disabled={Boolean(busyMessage)}
                            title="Localize Title, Description & Subtitles for this language"
                          >
                            Translate {lang.code.toUpperCase()}
                          </button>
                        </div>
                      </div>

                      <div className="asset-row">
                        <div>
                          <div style={{ fontWeight: 600, fontSize: "0.82rem" }}>
                            Metadata (Title & Desc)
                          </div>
                          <span
                            className={stateBadgeClass(
                              meta?.state ?? "pending"
                            )}
                          >
                            {meta?.state ?? "not generated"}
                          </span>
                        </div>
                        {meta && (
                          <button
                            className="btn"
                            onClick={() => void openAssetEditor(meta.id)}
                          >
                            Review / Edit
                          </button>
                        )}
                      </div>

                      <div className="asset-row">
                        <div>
                          <div style={{ fontWeight: 600, fontSize: "0.82rem" }}>
                            Subtitles ({cap?.caption_cue_count ?? 0} cues)
                          </div>
                          <span
                            className={stateBadgeClass(cap?.state ?? "pending")}
                          >
                            {cap?.state ?? "not generated"}
                          </span>
                          {cap?.provider_used && (
                            <span
                              className="brand-subtitle"
                              style={{ marginLeft: "0.4rem" }}
                            >
                              via {cap.provider_used}
                            </span>
                          )}
                        </div>
                        {cap && (
                          <button
                            className="btn"
                            onClick={() => void openAssetEditor(cap.id)}
                          >
                            Review / Edit
                          </button>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>

              {selectedAsset && (
                <section className="editor-panel">
                  <div
                    style={{
                      display: "flex",
                      flexWrap: "wrap",
                      justifyContent: "space-between",
                      alignItems: "center",
                      gap: "0.75rem",
                    }}
                  >
                    <div>
                      <h3 style={{ margin: 0 }}>
                        Reviewing {selectedAsset.language_name} (
                        {selectedAsset.native_name}) —{" "}
                        {selectedAsset.kind.toUpperCase()}
                        {selectedAsset.is_rtl && " [RTL]"}
                      </h3>
                      <div className="brand-subtitle">
                        State:{" "}
                        <span className={stateBadgeClass(selectedAsset.state)}>
                          {selectedAsset.state}
                        </span>{" "}
                        · Provider:{" "}
                        <code>{selectedAsset.provider_used ?? "n/a"}</code> ·
                        Prompt:{" "}
                        <code>{selectedAsset.prompt_version ?? "n/a"}</code> ·
                        Hash:{" "}
                        <code>
                          {selectedAsset.content_hash?.slice(0, 22) ?? "none"}
                        </code>
                      </div>
                    </div>
                    <button
                      className="btn"
                      onClick={() => setSelectedAsset(null)}
                    >
                      Close Editor
                    </button>
                  </div>

                  {/* Deterministic Validation & Quality Review Summary */}
                  <div className="side-by-side">
                    <div className="alert-banner">
                      <strong>
                        Deterministic Validation:{" "}
                        {selectedAsset.validation_passed ? "PASSED" : "FAILED"}
                      </strong>
                      {(selectedAsset.validation_report.issues ?? []).length >
                        0 && (
                        <ul className="issue-list">
                          {(selectedAsset.validation_report.issues ?? []).map(
                            (iss, idx) => (
                              <li key={idx}>
                                <code>{iss.code}</code>: {iss.message}
                              </li>
                            )
                          )}
                        </ul>
                      )}
                    </div>

                    <div className="alert-banner">
                      <strong>
                        Quality Review:{" "}
                        {selectedAsset.review_passed
                          ? "PASSED"
                          : "FLAGGED FOR HUMAN REVIEW"}
                      </strong>
                      <div className="brand-subtitle">
                        {selectedAsset.review_report.summary}
                      </div>
                      {(selectedAsset.review_report.issues ?? []).length >
                        0 && (
                        <ul className="issue-list">
                          {(selectedAsset.review_report.issues ?? []).map(
                            (iss, idx) => (
                              <li key={idx}>
                                <strong>[{iss.severity}]</strong> (
                                {iss.location}) {iss.message}
                              </li>
                            )
                          )}
                        </ul>
                      )}
                    </div>
                  </div>

                  {/* Side-by-side editor for Metadata OR Captions */}
                  {selectedAsset.kind === "metadata" ? (
                    <div className="side-by-side">
                      <div className="field-group">
                        <div className="field-label">
                          <span>Turkish Source Title</span>
                          <span>{selectedAsset.source_title.length} chars</span>
                        </div>
                        <input
                          className="input-box"
                          value={selectedAsset.source_title}
                          readOnly
                        />
                        <div className="field-label">
                          <span>Turkish Source Description</span>
                        </div>
                        <textarea
                          className="textarea-box"
                          value={selectedAsset.source_description}
                          readOnly
                        />
                      </div>

                      <div
                        className="field-group"
                        dir={selectedAsset.is_rtl ? "rtl" : "ltr"}
                      >
                        <div className="field-label" dir="ltr">
                          <span>
                            Localized Title ({selectedAsset.language_name})
                          </span>
                          <span
                            style={{
                              color:
                                editTitle.length > 100 ? "#dc2626" : undefined,
                            }}
                          >
                            {editTitle.length} / 100 chars
                          </span>
                        </div>
                        <input
                          className="input-box"
                          value={editTitle}
                          onChange={(e) => setEditTitle(e.target.value)}
                        />
                        <div className="field-label" dir="ltr">
                          <span>
                            Localized Description ({selectedAsset.language_name}
                            )
                          </span>
                          <span
                            style={{
                              color:
                                descriptionBytes > 5000 ? "#dc2626" : undefined,
                            }}
                          >
                            {descriptionBytes} / 5000 UTF-8 bytes
                          </span>
                        </div>
                        <textarea
                          className="textarea-box"
                          value={editDescription}
                          onChange={(e) => setEditDescription(e.target.value)}
                        />
                      </div>
                    </div>
                  ) : (
                    <div>
                      <div
                        style={{
                          display: "flex",
                          justifyContent: "space-between",
                          marginBottom: "0.5rem",
                        }}
                      >
                        <strong>
                          Side-by-Side Subtitle Cues ({editCues.length} cues)
                        </strong>
                        <button
                          className="btn"
                          onClick={() => {
                            if (!rawSrtMode) {
                              setEditRawSrt(buildSrtFromCues(editCues));
                            }
                            setRawSrtMode(!rawSrtMode);
                          }}
                        >
                          {rawSrtMode
                            ? "Switch to Cue Table View"
                            : "Switch to Raw SRT View"}
                        </button>
                      </div>

                      {rawSrtMode ? (
                        <div className="side-by-side">
                          <textarea
                            className="textarea-box"
                            value={selectedAsset.source_caption_content ?? ""}
                            readOnly
                          />
                          <textarea
                            className="textarea-box"
                            dir={selectedAsset.is_rtl ? "rtl" : "ltr"}
                            value={editRawSrt}
                            onChange={(e) => setEditRawSrt(e.target.value)}
                          />
                        </div>
                      ) : (
                        <div
                          style={{
                            maxHeight: 380,
                            overflowY: "auto",
                            border: "1px solid var(--border-color)",
                            borderRadius: "0.4rem",
                          }}
                        >
                          <table className="cue-table">
                            <thead>
                              <tr>
                                <th style={{ width: 52 }}>#</th>
                                <th style={{ width: 190 }}>Timing</th>
                                <th>Turkish Source Cue</th>
                                <th>
                                  Translated Cue ({selectedAsset.language_name})
                                </th>
                              </tr>
                            </thead>
                            <tbody>
                              {editCues.map((cue, idx) => (
                                <tr key={cue.index}>
                                  <td>{cue.index}</td>
                                  <td>
                                    <code>
                                      {formatSrtTime(cue.start_ms)} &rarr;{" "}
                                      {formatSrtTime(cue.end_ms)}
                                    </code>
                                  </td>
                                  <td>{cue.source_text}</td>
                                  <td
                                    dir={selectedAsset.is_rtl ? "rtl" : "ltr"}
                                  >
                                    <input
                                      className="input-box"
                                      value={cue.translated_text}
                                      onChange={(e) => {
                                        const next = [...editCues];
                                        next[idx] = {
                                          ...cue,
                                          translated_text: e.target.value,
                                        };
                                        setEditCues(next);
                                      }}
                                    />
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      )}
                    </div>
                  )}

                  {/* Save, Approve, Reject Controls */}
                  <div
                    style={{
                      display: "flex",
                      flexWrap: "wrap",
                      alignItems: "center",
                      justifyContent: "space-between",
                      gap: "0.75rem",
                      paddingTop: "0.5rem",
                      borderTop: "1px solid var(--border-color)",
                    }}
                  >
                    <div style={{ display: "flex", gap: "0.5rem" }}>
                      <button
                        className="btn btn-primary"
                        onClick={() => void handleSaveAssetEdits()}
                        disabled={Boolean(busyMessage)}
                      >
                        Save Edits & Re-Validate
                      </button>
                    </div>

                    <div
                      style={{
                        display: "flex",
                        flexWrap: "wrap",
                        alignItems: "center",
                        gap: "0.5rem",
                      }}
                    >
                      {(!selectedAsset.review_passed ||
                        selectedAsset.source_ambiguous) && (
                        <label
                          style={{
                            fontSize: "0.8rem",
                            display: "inline-flex",
                            alignItems: "center",
                            gap: "0.3rem",
                          }}
                        >
                          <input
                            type="checkbox"
                            checked={overrideReview}
                            onChange={(e) =>
                              setOverrideReview(e.target.checked)
                            }
                          />
                          Confirm human sign-off on review warnings
                        </label>
                      )}
                      <input
                        className="input-box"
                        style={{ width: 200 }}
                        placeholder="Optional approval note..."
                        value={approvalNotes}
                        onChange={(e) => setApprovalNotes(e.target.value)}
                      />
                      <button
                        className="btn btn-success"
                        onClick={() => void handleApproveAsset()}
                        disabled={
                          Boolean(busyMessage) ||
                          !selectedAsset.validation_passed
                        }
                      >
                        Approve (Bind to Hash)
                      </button>
                      <button
                        className="btn btn-danger"
                        onClick={() => void handleRejectAsset()}
                        disabled={Boolean(busyMessage)}
                      >
                        Reject
                      </button>
                      {selectedAsset.state === "uncertain" && (
                        <button
                          className="btn btn-primary"
                          onClick={() => void handleReconcileAsset()}
                          disabled={Boolean(busyMessage)}
                        >
                          Reconcile Uncertain Outcome
                        </button>
                      )}
                      <button
                        className="btn btn-primary"
                        onClick={() => void handlePublishAsset()}
                        disabled={
                          Boolean(busyMessage) ||
                          !selectedAsset.publish_decision.allowed
                        }
                        title={selectedAsset.publish_decision.message}
                      >
                        Publish to YouTube
                      </button>
                    </div>
                  </div>

                  {/* Active Approval & Policy Decision Preview */}
                  <div className="alert-banner">
                    <div>
                      <strong>Active Approval:</strong>{" "}
                      {selectedAsset.active_approval ? (
                        <span>
                          Approved by{" "}
                          <code>
                            {selectedAsset.active_approval.approved_by}
                          </code>{" "}
                          at {selectedAsset.active_approval.approved_at} (bound
                          to{" "}
                          <code>
                            {selectedAsset.active_approval.approved_content_hash.slice(
                              0,
                              20
                            )}
                            ...
                          </code>
                          )
                        </span>
                      ) : (
                        <span>No active approval for current content hash</span>
                      )}
                    </div>
                    <div>
                      <strong>Publishing Policy Engine Preview:</strong>{" "}
                      <code>{selectedAsset.publish_decision.reason_code}</code>{" "}
                      — {selectedAsset.publish_decision.message}
                    </div>
                    {selectedAsset.youtube_published_caption_id && (
                      <div>
                        <strong>Verified YouTube Caption ID:</strong>{" "}
                        <code>{selectedAsset.youtube_published_caption_id}</code>
                      </div>
                    )}
                  </div>

                  {/* Stage 7 Publish Attempt History */}
                  {selectedAsset.publish_attempts.length > 0 && (
                    <div>
                      <strong>
                        Publish Attempt History (
                        {selectedAsset.publish_attempts.length})
                      </strong>
                      <table
                        className="cue-table"
                        style={{ marginTop: "0.4rem" }}
                      >
                        <thead>
                          <tr>
                            <th>ID</th>
                            <th>Operation</th>
                            <th>Outcome</th>
                            <th>Idempotency Key</th>
                            <th>Verified At</th>
                            <th>Reconciled At</th>
                            <th>Summary / Error</th>
                          </tr>
                        </thead>
                        <tbody>
                          {selectedAsset.publish_attempts.map((att) => (
                            <tr key={att.id}>
                              <td>{att.id}</td>
                              <td>
                                <code>{att.operation}</code>
                              </td>
                              <td>
                                <span
                                  className={
                                    att.outcome === "succeeded"
                                      ? "badge badge-success"
                                      : att.outcome === "uncertain"
                                        ? "badge badge-warning"
                                        : "badge badge-danger"
                                  }
                                >
                                  {att.outcome}
                                </span>
                              </td>
                              <td>
                                <code style={{ fontSize: "0.72rem" }}>
                                  {att.idempotency_key}
                                </code>
                              </td>
                              <td>{att.verified_at ?? "—"}</td>
                              <td>{att.reconciled_at ?? "—"}</td>
                              <td>
                                <code style={{ fontSize: "0.72rem" }}>
                                  {att.error_message ??
                                    JSON.stringify(att.response_summary)}
                                </code>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </section>
              )}
            </>
          )}

          {activeTab === "settings" && policySettings && (
            <section className="editor-panel">
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                }}
              >
                <div>
                  <h2 style={{ margin: 0 }}>
                    Per-Language & Global Publishing Policy
                  </h2>
                  <p className="brand-subtitle" style={{ margin: 0 }}>
                    While Global Auto-Publish is OFF, all languages configured
                    as <code>auto_after_validation</code> behave as{" "}
                    <code>approval_required</code>. Every change is recorded in
                    the audit log.
                  </p>
                </div>
                <button
                  className="btn"
                  onClick={() =>
                    void handleGlobalAutoPublishToggle(
                      !policySettings.global_auto_publish_enabled
                    )
                  }
                >
                  {policySettings.global_auto_publish_enabled
                    ? "Disable Global Auto-Publish"
                    : "Enable Global Auto-Publish"}
                </button>
              </div>

              <table className="cue-table">
                <thead>
                  <tr>
                    <th>Code</th>
                    <th>Language</th>
                    <th>Direction</th>
                    <th>Enabled</th>
                    <th>Configured Mode</th>
                    <th>Effective Mode</th>
                  </tr>
                </thead>
                <tbody>
                  {policySettings.languages.map((lang) => (
                    <tr key={lang.code}>
                      <td>
                        <code>{lang.code}</code>
                      </td>
                      <td>
                        {lang.name} ({lang.native_name})
                      </td>
                      <td>{lang.rtl ? "RTL (Right-to-Left)" : "LTR"}</td>
                      <td>
                        <input
                          type="checkbox"
                          checked={lang.enabled}
                          onChange={(e) =>
                            void handleLanguagePolicyChange(lang.code, {
                              enabled: e.target.checked,
                            })
                          }
                        />
                      </td>
                      <td>
                        <select
                          className="input-box"
                          value={lang.configured_mode}
                          onChange={(e) =>
                            void handleLanguagePolicyChange(lang.code, {
                              mode: e.target.value as PublishMode,
                            })
                          }
                        >
                          <option value="approval_required">
                            approval_required
                          </option>
                          <option value="auto_after_validation">
                            auto_after_validation
                          </option>
                          <option value="disabled">disabled</option>
                        </select>
                      </td>
                      <td>
                        <span className="badge">{lang.effective_mode}</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          )}

          {activeTab === "audit" && (
            <section className="editor-panel">
              <h2 style={{ margin: 0 }}>Immutable Audit Log</h2>
              <table className="cue-table">
                <thead>
                  <tr>
                    <th>ID</th>
                    <th>Timestamp (UTC)</th>
                    <th>Actor</th>
                    <th>Action</th>
                    <th>Target</th>
                    <th>Details</th>
                  </tr>
                </thead>
                <tbody>
                  {auditEvents.map((ev) => (
                    <tr key={ev.id}>
                      <td>{ev.id}</td>
                      <td>{ev.created_at}</td>
                      <td>
                        <code>{ev.actor}</code>
                      </td>
                      <td>
                        <strong>{ev.action}</strong>
                      </td>
                      <td>
                        {ev.target_type}:{ev.target_id}
                      </td>
                      <td>
                        <code style={{ fontSize: "0.75rem" }}>
                          {JSON.stringify(ev.details)}
                        </code>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          )}
        </main>
      </div>
    </div>
  );
}
