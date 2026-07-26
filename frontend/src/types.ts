export type FsEntry = {
  name: string;
  path: string;
  is_dir: boolean;
};

export type BrowseResponse = {
  current_path?: string;
  parent_path?: string | null;
  entries: FsEntry[];
};

export type SpectrumClass = {
  class_key: string;
  class_display_name: string;
  component_count: number;
  total_count: number;
  active_count: number;
  excluded_count: number;
};

export type LoadingMeta = {
  status: "ready" | "building";
  progress_message: string | null;
};

export type AxisKind = "wavelength" | "wavenumber";

export type AxisSummary = {
  axis_kind: AxisKind;
  axis_unit: string;
  count: number;
};

export type SpectrumSummary = LoadingMeta & {
  total_count: number;
  axis_summary: AxisSummary[];
};

export type SpectrumItem = {
  id: number;
  file_name: string;
  source_path_last_seen: string;
  content_sha256?: string | null;
  metadata: Record<string, unknown>;
  axis_kind: AxisKind | string;
  axis_unit: string;
  point_count: number;
  x_values: number[];
  y_values: number[];
  labels: Array<{ name: string; value: number }>;
  class_key: string;
  class_display_name: string;
  component_count: number;
  is_excluded: boolean;
  excluded_at: string | null;
  created_at: string | null;
  updated_at: string | null;
};

export type JobItem = {
  id: number;
  type: string;
  status: string;
  params: Record<string, unknown>;
  stats: Record<string, unknown>;
  log_text: string;
  progress_message: string;
  total_discovered: number;
  processed_count: number;
  imported_count: number;
  skipped_count: number;
  failed_count: number;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  updated_at: string | null;
};

export type SubsetSummary = {
  subset_id: string;
  index: number;
  count: number;
};
