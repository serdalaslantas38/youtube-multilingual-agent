export type PublishMode =
  | "auto_after_validation"
  | "approval_required"
  | "disabled";

export type AssetKind = "metadata" | "caption";

export type AssetState =
  | "pending"
  | "awaiting_source"
  | "translating"
  | "validating"
  | "reviewing"
  | "needs_attention"
  | "awaiting_approval"
  | "ready_to_publish"
  | "publishing"
  | "verifying"
  | "uncertain"
  | "published"
  | "skipped"
  | "failed";

export interface ChannelStatusResponse {
  connected: boolean;
  configured_channel_id: string;
  youtube_channel_id: string | null;
  channel_title: string | null;
  custom_url: string | null;
  uploads_playlist_id: string | null;
  status: string;
  scopes: string[];
  token_warning: string | null;
  connected_at: string | null;
  last_verified_at: string | null;
  today_quota_units_used: number;
  daily_quota_limit_estimate: number;
}

export interface DiscoveredVideoSummary {
  youtube_video_id: string;
  youtube_channel_id: string;
  source_language: string;
  default_audio_language: string | null;
  source_title: string;
  source_description: string;
  privacy_status: "public" | "private" | "unlisted";
  scheduled_publish_at: string | null;
  published_at: string | null;
  workflow_state: string;
  existing_localization_languages: string[];
  discovered_at: string;
}

export interface LanguageAssetSummary {
  id: number;
  youtube_video_id: string;
  language: string;
  kind: AssetKind;
  state: AssetState;
  provider_used: string | null;
  prompt_version: string | null;
  translated_title: string | null;
  translated_description: string | null;
  caption_format: string | null;
  caption_cue_count: number | null;
  content_hash: string | null;
  validation_passed: boolean | null;
  validation_report: {
    passed?: boolean;
    issues?: Array<{ code: string; message: string; blocking: boolean }>;
    cue_count?: number;
  };
  review_passed: boolean | null;
  source_ambiguous: boolean;
  review_report: {
    passed?: boolean;
    source_ambiguous?: boolean;
    summary?: string;
    issues?: Array<{
      category: string;
      severity: "info" | "warning" | "blocking";
      message: string;
      location: string;
    }>;
  };
  attempt_count: number;
  last_error: string | null;
  updated_at: string;
}

export interface VideoLocalizationResponse {
  youtube_video_id: string;
  source_title: string;
  workflow_state: string;
  source_caption_ready: boolean;
  selected_source_caption_id: string | null;
  selected_source_track_kind: string | null;
  assets: LanguageAssetSummary[];
  quota_units_used_this_run: number;
}

export interface AlignedCuePair {
  index: number;
  start_ms: number;
  end_ms: number;
  source_text: string;
  translated_text: string;
}

export interface ApprovalRecordSummary {
  id: number;
  approved_content_hash: string;
  approved_by: string;
  notes: string | null;
  approved_at: string;
  invalidated_at: string | null;
  invalidation_reason: string | null;
  is_valid_for_current_hash: boolean;
}

export interface PublishDecisionPreview {
  allowed: boolean;
  effective_mode: PublishMode;
  reason_code: string;
  message: string;
}

export interface PublishAttemptRecordSummary {
  id: number;
  asset_id: number;
  idempotency_key: string;
  operation: string;
  request_digest: string;
  outcome: "succeeded" | "failed" | "uncertain";
  response_summary: Record<string, unknown>;
  error_message: string | null;
  created_at: string;
  verified_at: string | null;
  reconciled_at: string | null;
}

export interface PublishAssetResponse {
  asset: AssetDetailResponse;
  attempt: PublishAttemptRecordSummary | null;
  idempotent_reused: boolean;
  quota_units_used: number;
}

export interface BatchPublishVideoResponse {
  youtube_video_id: string;
  workflow_state: string;
  published_asset_ids: number[];
  skipped_asset_ids: number[];
  uncertain_asset_ids: number[];
  failed_asset_ids: number[];
  blocked_reasons: Record<string, string>;
  quota_units_used: number;
}

export interface AssetDetailResponse {
  id: number;
  youtube_video_id: string;
  language: string;
  language_name: string;
  native_name: string;
  is_rtl: boolean;
  kind: AssetKind;
  state: AssetState;
  provider_used: string | null;
  prompt_version: string | null;
  source_title: string;
  source_description: string;
  source_caption_content: string | null;
  translated_title: string | null;
  translated_description: string | null;
  caption_content: string | null;
  caption_format: string | null;
  youtube_published_caption_id: string | null;
  aligned_cues: AlignedCuePair[];
  content_hash: string | null;
  validation_passed: boolean | null;
  validation_report: {
    passed?: boolean;
    issues?: Array<{ code: string; message: string; blocking: boolean }>;
    cue_count?: number;
  };
  review_passed: boolean | null;
  source_ambiguous: boolean;
  review_report: {
    passed?: boolean;
    source_ambiguous?: boolean;
    summary?: string;
    issues?: Array<{
      category: string;
      severity: "info" | "warning" | "blocking";
      message: string;
      location: string;
    }>;
  };
  active_approval: ApprovalRecordSummary | null;
  approval_history: ApprovalRecordSummary[];
  publish_decision: PublishDecisionPreview;
  publish_attempts: PublishAttemptRecordSummary[];
  last_error: string | null;
  updated_at: string;
}

export interface LanguagePolicyItem {
  code: string;
  name: string;
  native_name: string;
  caption_tlang: string;
  rtl: boolean;
  enabled: boolean;
  configured_mode: PublishMode;
  effective_mode: PublishMode;
}

export interface PolicySettingsResponse {
  source_language_code: string;
  source_language_name: string;
  youtube_publishing_enabled: boolean;
  global_auto_publish_enabled: boolean;
  default_mode: PublishMode;
  protected_glossary: string[];
  translation_sources: string[];
  languages: LanguagePolicyItem[];
}

export interface DashboardOperatorResponse {
  authenticated: boolean;
  user_id: string;
  email: string;
  organization_id: string | null;
  organization_name: string | null;
  role: string | null;
  csrf_token: string | null;
  organizations: OrganizationMembershipItem[];
}

export interface OrganizationMembershipItem {
  id: string;
  name: string;
  role: string;
}

export interface AuditEventItem {
  id: number;
  actor: string;
  action: string;
  target_type: string;
  target_id: string;
  details: Record<string, unknown>;
  created_at: string;
}

