export type Prefs = {
  destination: string;
  days: number;
  people: number;
  date: string;
  origin: string;
  budget: string;
  include_arrival: boolean;
  interests: string[];
  pace: string;
  notes: string;
  selection: string;
  transcription: string;
  links: string[];
};
export type Source = {
  id: string;
  meta: {
    title: string;
    author: string;
    duration: number | null;
    published: string | null;
    url: string;
    part: number;
  };
  selected: number;
  status: string;
  reason: string;
  error: string;
  complete: number;
  has_transcript: boolean;
};
export type Task = {
  version_selection?: {
    latest_version: number;
    baseline_version: number;
    baseline_score: number;
    baseline_reason: string;
  };
  evidence_searches?: {
    issue_id: string;
    status: string;
    complete: boolean;
    scope: { items_scanned: number };
    hits: unknown[];
  }[];
  revision_issues?: {
    issue_id: string;
    description: string;
    status: string;
    review_status?: string;
    targets: string[];
  }[];
  request_metrics?: {
    context_used: number;
    context_limit: number;
    output_used: number | null;
    output_limit: number;
  };
  resources?: { label: string; used: number; limit: number; remaining: number }[];
  request_limits?: { output_tokens: number; context_budget: number };
  quality_cycle?: {
    phase: string;
    revision_round: number;
    history: {
      version: number;
      round: number;
      score: number;
      repairable: string[];
      evidence_gaps: string[];
      next_phase: string;
      changed_sections?: string[];
    }[];
  };
  id: string;
  status: string;
  stage: string;
  prefs: Prefs;
  created: number;
  updated: number;
  error: string;
  source_count?: number;
  version?: number;
  owner?: string;
  unknown_requests?: number;
  video_limits: { batch: number; total: number };
  sources: Source[];
  events: { id: number; stage: string; message: string; created: number; level: string }[];
  versions: { version: number; model: string; created: number }[];
  usage: {
    provider: string;
    requests: number;
    seconds: number;
    tokens: number | null;
    cost: number | null;
    unknown_cost: number;
    unknown_tokens: number;
  }[];
  chunks: {
    id: string;
    source_id: string;
    start: number;
    end: number;
    status: string;
    error: string;
  }[];
};
export type Config = Record<string, string | number | boolean | null>;
export type Guide = {
  markdown: string;
  review_markdown?: string;
  version: number;
  model: string;
  created: number;
  content: {
    quality_review?: {
      score: number;
      passed: boolean;
      processed_videos: number;
      stop_reason?: string;
    };
    warnings: string[];
    source_ids: string[];
  };
};
