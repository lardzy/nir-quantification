import type { AxisKind, AxisSummary, BrowseResponse, JobItem, LoadingMeta, SpectrumClass, SpectrumItem, SpectrumSummary, SubsetSummary } from "./types";

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `HTTP ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export const api = {
  async getRoots(): Promise<{ import_roots: string[]; export_roots: string[] }> {
    return request("/api/fs/roots");
  },
  async browse(kind: "import" | "export", path?: string): Promise<BrowseResponse> {
    const query = path ? `?kind=${kind}&path=${encodeURIComponent(path)}` : `?kind=${kind}`;
    return request(`/api/fs/browse${query}`);
  },
  async createImportJob(rootPath: string, validateLabels = true): Promise<JobItem> {
    return request("/api/import-jobs", {
      method: "POST",
      body: JSON.stringify({ root_path: rootPath, recursive: true, validate_labels: validateLabels })
    });
  },
  async createExportJob(exportRoot: string, scope: "active" | "excluded" | "all", classKeys: string[]): Promise<JobItem> {
    return request("/api/export-jobs", {
      method: "POST",
      body: JSON.stringify({ export_root: exportRoot, scope, class_keys: classKeys })
    });
  },
  async listJobs(): Promise<JobItem[]> {
    const data = await request<{ items: JobItem[] }>("/api/jobs?limit=20");
    return data.items;
  },
  async getClasses(
    sort: "count" | "component_count" | "name",
    options?: { signal?: AbortSignal }
  ): Promise<{ items: SpectrumClass[]; meta: LoadingMeta }> {
    return request<{ items: SpectrumClass[]; meta: LoadingMeta }>(`/api/classes?sort=${sort}`, { signal: options?.signal });
  },
  async getSpectraSummary(
    params: {
      classKey?: string;
      excluded: "active" | "excluded" | "all";
      subsetId?: string;
    },
    options?: { signal?: AbortSignal }
  ): Promise<SpectrumSummary> {
    const query = new URLSearchParams();
    if (params.classKey) query.set("class_key", params.classKey);
    if (params.subsetId) query.set("subset_id", params.subsetId);
    query.set("excluded", params.excluded);
    return request(`/api/spectra/summary?${query.toString()}`, { signal: options?.signal });
  },
  async getSpectra(params: {
    classKey?: string;
    excluded: "active" | "excluded" | "all";
    axisKind?: AxisKind;
    subsetId?: string;
    limit?: number;
  }, options?: { signal?: AbortSignal }): Promise<{
    items: SpectrumItem[];
    count: number;
    limit: number;
    axis_summary: AxisSummary[];
    point_budget?: number;
    returned_point_count?: number;
    truncated_by_point_budget?: boolean;
  }> {
    const query = new URLSearchParams();
    if (params.classKey) query.set("class_key", params.classKey);
    if (params.axisKind) query.set("axis_kind", params.axisKind);
    if (params.subsetId) query.set("subset_id", params.subsetId);
    query.set("excluded", params.excluded);
    query.set("limit", String(params.limit ?? 500));
    return request(`/api/spectra?${query.toString()}`, { signal: options?.signal });
  },
  async excludeSpectrum(id: number): Promise<SpectrumItem> {
    return request(`/api/spectra/${id}/exclude`, { method: "POST" });
  },
  async restoreSpectrum(id: number): Promise<SpectrumItem> {
    return request(`/api/spectra/${id}/restore`, { method: "POST" });
  },
  async recentExcluded(): Promise<SpectrumItem[]> {
    const data = await request<{ items: SpectrumItem[] }>("/api/excluded/recent?limit=50");
    return data.items;
  },
  async createSubsets(classKey: string, payload: { mode: "count" | "ratio"; parts: number }): Promise<{
    class_key: string;
    mode: string;
    subsets: SubsetSummary[];
  }> {
    return request(`/api/classes/${encodeURIComponent(classKey)}/subsets`, {
      method: "POST",
      body: JSON.stringify(payload)
    });
  }
};

export function subscribeJob(jobId: number, onMessage: (job: JobItem) => void): () => void {
  const source = new EventSource(`/api/jobs/${jobId}/events`);
  source.onmessage = (event) => onMessage(JSON.parse(event.data) as JobItem);
  source.onerror = () => source.close();
  return () => source.close();
}
