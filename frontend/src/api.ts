import type {
  AssetDetailResponse,
  AuditEventItem,
  BatchPublishVideoResponse,
  ChannelStatusResponse,
  DashboardOperatorResponse,
  DiscoveredVideoSummary,
  PolicySettingsResponse,
  PublishAssetResponse,
  PublishMode,
  VideoLocalizationResponse,
} from "./types";

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
    ...init,
  });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const errBody = (await response.json()) as {
        detail?: string | { reason_code?: string; message?: string };
      };
      if (typeof errBody?.detail === "string") {
        detail = errBody.detail;
      } else if (errBody?.detail && typeof errBody.detail === "object") {
        detail = `${errBody.detail.reason_code ?? "blocked"}: ${
          errBody.detail.message ?? JSON.stringify(errBody.detail)
        }`;
      }
    } catch {
      // ignore JSON parse failure on error body
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

export const dashboardApi = {
  getOperator(): Promise<DashboardOperatorResponse> {
    return requestJson<DashboardOperatorResponse>("/api/dashboard/auth/me");
  },
  loginOperator(email: string): Promise<DashboardOperatorResponse> {
    return requestJson<DashboardOperatorResponse>("/api/dashboard/auth/login", {
      method: "POST",
      body: JSON.stringify({ email }),
    });
  },
  logoutOperator(): Promise<DashboardOperatorResponse> {
    return requestJson<DashboardOperatorResponse>("/api/dashboard/auth/logout", {
      method: "POST",
    });
  },
  getChannelStatus(): Promise<ChannelStatusResponse> {
    return requestJson<ChannelStatusResponse>("/api/channel/status");
  },
  listVideos(): Promise<DiscoveredVideoSummary[]> {
    return requestJson<DiscoveredVideoSummary[]>("/api/videos");
  },
  discoverVideos(maxResults = 10): Promise<{
    channel_id: string;
    discovered_count: number;
    videos: DiscoveredVideoSummary[];
    today_quota_units_used: number;
  }> {
    return requestJson(`/api/videos/discover?max_results=${maxResults}`, {
      method: "POST",
    });
  },
  inspectVideoCaptions(youtubeVideoId: string): Promise<unknown> {
    return requestJson(`/api/videos/${encodeURIComponent(youtubeVideoId)}/captions/inspect`, {
      method: "POST",
    });
  },
  getVideoAssets(youtubeVideoId: string): Promise<VideoLocalizationResponse> {
    return requestJson<VideoLocalizationResponse>(
      `/api/videos/${encodeURIComponent(youtubeVideoId)}/assets`
    );
  },
  localizeVideo(
    youtubeVideoId: string,
    options: {
      languages?: string[];
      include_metadata?: boolean;
      include_captions?: boolean;
      force_regenerate?: boolean;
    } = {}
  ): Promise<VideoLocalizationResponse> {
    return requestJson<VideoLocalizationResponse>(
      `/api/videos/${encodeURIComponent(youtubeVideoId)}/localize`,
      {
        method: "POST",
        body: JSON.stringify(options),
      }
    );
  },
  getAssetDetail(assetId: number): Promise<AssetDetailResponse> {
    return requestJson<AssetDetailResponse>(`/api/assets/${assetId}`);
  },
  editAsset(
    assetId: number,
    payload: {
      translated_title?: string;
      translated_description?: string;
      caption_content?: string;
    }
  ): Promise<AssetDetailResponse> {
    return requestJson<AssetDetailResponse>(`/api/assets/${assetId}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
  },
  approveAsset(
    assetId: number,
    payload: {
      expected_content_hash: string;
      notes?: string;
      override_review_issues?: boolean;
    }
  ): Promise<AssetDetailResponse> {
    return requestJson<AssetDetailResponse>(`/api/assets/${assetId}/approve`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  rejectAsset(assetId: number, reason: string): Promise<AssetDetailResponse> {
    return requestJson<AssetDetailResponse>(`/api/assets/${assetId}/reject`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    });
  },
  publishAsset(
    assetId: number,
    payload: {
      expected_content_hash?: string;
      is_draft?: boolean;
      allow_set_default_language?: boolean;
    } = {}
  ): Promise<PublishAssetResponse> {
    return requestJson<PublishAssetResponse>(`/api/assets/${assetId}/publish`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  reconcileAsset(
    assetId: number,
    notes?: string
  ): Promise<PublishAssetResponse> {
    return requestJson<PublishAssetResponse>(
      `/api/assets/${assetId}/reconcile`,
      {
        method: "POST",
        body: JSON.stringify({ notes }),
      }
    );
  },
  publishReadyAssetsForVideo(
    youtubeVideoId: string,
    payload: {
      languages?: string[];
      include_metadata?: boolean;
      include_captions?: boolean;
      is_draft?: boolean;
    } = {}
  ): Promise<BatchPublishVideoResponse> {
    return requestJson<BatchPublishVideoResponse>(
      `/api/videos/${encodeURIComponent(youtubeVideoId)}/publish-ready`,
      {
        method: "POST",
        body: JSON.stringify(payload),
      }
    );
  },
  getPolicySettings(): Promise<PolicySettingsResponse> {
    return requestJson<PolicySettingsResponse>("/api/settings/policy");
  },
  updateLanguagePolicy(
    languageCode: string,
    payload: {
      mode?: PublishMode;
      enabled?: boolean;
      name?: string;
      native_name?: string;
      caption_tlang?: string;
      rtl?: boolean;
    }
  ): Promise<PolicySettingsResponse> {
    return requestJson<PolicySettingsResponse>(
      `/api/settings/policy/languages/${encodeURIComponent(languageCode)}`,
      {
        method: "PUT",
        body: JSON.stringify(payload),
      }
    );
  },
  updateGlobalPolicy(
    autoPublishEnabled: boolean,
    reason: string
  ): Promise<PolicySettingsResponse> {
    return requestJson<PolicySettingsResponse>("/api/settings/policy/global", {
      method: "PUT",
      body: JSON.stringify({
        auto_publish_enabled: autoPublishEnabled,
        reason,
      }),
    });
  },
  listAuditEvents(limit = 50): Promise<AuditEventItem[]> {
    return requestJson<AuditEventItem[]>(`/api/audit-events?limit=${limit}`);
  },
};

