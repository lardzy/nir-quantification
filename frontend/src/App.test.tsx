import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { api, subscribeJob } from "./api";
import type { AxisSummary } from "./types";

const baseSpectrum = {
  id: 101,
  file_name: "sample-spectrum.csv",
  source_path_last_seen: "/workspace/imports/sample-spectrum.csv",
  metadata: { device_serial: "SN-001", scan_config: "Hadamard 1" },
  axis_kind: "wavelength",
  axis_unit: "nm",
  point_count: 228,
  x_values: [900, 901, 902],
  y_values: [0.1, 0.2, 0.18],
  labels: [
    { name: "棉", value: 60 },
    { name: "锦纶", value: 40 }
  ],
  class_key: "棉|锦纶",
  class_display_name: "棉、锦纶",
  component_count: 2,
  is_excluded: false,
  excluded_at: null,
  created_at: null,
  updated_at: null
};

const baseClass = {
  class_key: "棉|锦纶",
  class_display_name: "棉、锦纶",
  component_count: 2,
  total_count: 2,
  active_count: 2,
  excluded_count: 0
};

const fourierSpectrum = {
  ...baseSpectrum,
  id: 151,
  file_name: "fourier-spectrum.csv",
  source_path_last_seen: "/workspace/imports/fourier-spectrum.csv",
  metadata: {
    sample_id: "230340227",
    acquisition_date: "2025-09-09",
    acquisition_time: "08:38:55",
    part_name: "A"
  },
  axis_kind: "wavenumber",
  axis_unit: "cm^-1",
  x_values: [3999.64, 4003.497, 4007.354]
};

const secondFourierSpectrum = {
  ...fourierSpectrum,
  id: 152,
  file_name: "fourier-spectrum-2.csv",
  source_path_last_seen: "/workspace/imports/fourier-spectrum-2.csv"
};

const secondSpectrum = {
  ...baseSpectrum,
  id: 202,
  file_name: "polyester-spectrum.csv",
  source_path_last_seen: "/workspace/imports/polyester-spectrum.csv",
  labels: [{ name: "聚酯纤维", value: 100 }],
  class_key: "聚酯纤维",
  class_display_name: "聚酯纤维",
  component_count: 1
};

const secondClass = {
  class_key: "聚酯纤维",
  class_display_name: "聚酯纤维",
  component_count: 1,
  total_count: 1,
  active_count: 1,
  excluded_count: 0
};

const wavelengthAxisSummary: AxisSummary[] = [{ axis_kind: "wavelength", axis_unit: "nm", count: 1 }];
const wavenumberAxisSummary: AxisSummary[] = [{ axis_kind: "wavenumber", axis_unit: "cm^-1", count: 1 }];
const mixedAxisSummary: AxisSummary[] = [
  { axis_kind: "wavelength", axis_unit: "nm", count: 1 },
  { axis_kind: "wavenumber", axis_unit: "cm^-1", count: 1 }
];

vi.mock("./SpectrumChart", () => ({
  SpectrumChart: ({
    spectra,
    resetSignal: _resetSignal,
    onLockSpectrum,
    onQuickExclude,
    interactionMode
  }: {
    spectra: Array<typeof baseSpectrum>;
    resetSignal: number;
    onLockSpectrum: (spectrum: typeof baseSpectrum | null) => void;
    onQuickExclude: (spectrum: typeof baseSpectrum) => void;
    interactionMode: "full" | "medium" | "dense";
  }) => (
    <div data-testid="mock-chart" data-mode={interactionMode}>
      {spectra.map((spectrum) => (
        <div key={spectrum.id}>
          <button onClick={() => onLockSpectrum(spectrum)}>{spectrum.file_name}</button>
          <button aria-label={`quick-exclude-${spectrum.file_name}`} onClick={() => onQuickExclude(spectrum)}>
            quick exclude
          </button>
        </div>
      ))}
    </div>
  )
}));

vi.mock("./api", () => ({
  api: {
    getRoots: vi.fn(),
    browse: vi.fn(),
    createImportJob: vi.fn(),
    createExportJob: vi.fn(),
    listJobs: vi.fn(),
    getClasses: vi.fn(),
    getSpectraSummary: vi.fn(),
    getSpectra: vi.fn(),
    excludeSpectrum: vi.fn(),
    restoreSpectrum: vi.fn(),
    recentExcluded: vi.fn(),
    createSubsets: vi.fn()
  },
  subscribeJob: vi.fn(() => () => undefined)
}));

describe("App", () => {
  beforeEach(() => {
    let excluded = false;
    vi.clearAllMocks();
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      writable: true,
      value: 1440
    });

    vi.mocked(api.getRoots).mockResolvedValue({
      import_roots: ["/workspace/imports"],
      export_roots: ["/workspace/exports"]
    });
    vi.mocked(api.browse).mockImplementation(async (_kind, path) => ({
      current_path: path,
      parent_path: null,
      entries: []
    }));
    vi.mocked(api.listJobs).mockResolvedValue([]);
    vi.mocked(api.createImportJob).mockResolvedValue({
      id: 1,
      type: "import",
      status: "pending",
      params: {},
      stats: {},
      log_text: "",
      progress_message: "Queued",
      total_discovered: 0,
      processed_count: 0,
      imported_count: 0,
      skipped_count: 0,
      failed_count: 0,
      created_at: null,
      started_at: null,
      finished_at: null,
      updated_at: null
    });
    vi.mocked(api.createExportJob).mockResolvedValue({
      id: 2,
      type: "export",
      status: "pending",
      params: {},
      stats: {},
      log_text: "",
      progress_message: "Queued",
      total_discovered: 0,
      processed_count: 0,
      imported_count: 0,
      skipped_count: 0,
      failed_count: 0,
      created_at: null,
      started_at: null,
      finished_at: null,
      updated_at: null
    });
    vi.mocked(api.getClasses).mockImplementation(async () => ({
      items: [
        {
          ...baseClass,
          active_count: excluded ? 1 : 2,
          excluded_count: excluded ? 1 : 0
        },
        secondClass
      ],
      meta: { status: "ready", progress_message: null }
    }));
    vi.mocked(api.getSpectraSummary).mockImplementation(async ({ classKey, excluded: filter }) => {
      if (classKey === secondClass.class_key) {
        const axisSummary: AxisSummary[] = filter === "excluded" ? [] : wavelengthAxisSummary;
        return {
          status: "ready",
          progress_message: null,
          total_count: filter === "excluded" ? 0 : 1,
          axis_summary: axisSummary
        };
      }

      const axisSummary: AxisSummary[] =
        filter === "excluded"
          ? excluded
            ? wavelengthAxisSummary
            : []
          : filter === "all"
            ? mixedAxisSummary
            : excluded
              ? wavenumberAxisSummary
              : mixedAxisSummary;
      const totalCount =
        filter === "excluded"
          ? excluded
            ? 1
            : 0
          : filter === "all"
            ? 2
            : excluded
              ? 1
              : 2;
      return {
        status: "ready",
        progress_message: null,
        total_count: totalCount,
        axis_summary: axisSummary
      };
    });
    vi.mocked(api.getSpectra).mockImplementation(async ({ classKey, excluded: filter, axisKind, limit }) => {
      if (classKey === secondClass.class_key) {
        const items =
          filter === "active" || filter === "all"
            ? [{ ...secondSpectrum, is_excluded: false }]
            : [];
        return { items, count: items.length, limit: limit ?? 2000, axis_summary: wavelengthAxisSummary };
      }

      const wavelengthSpectrum = { ...baseSpectrum, is_excluded: excluded };
      const wavenumberSpectrum = { ...fourierSpectrum, is_excluded: false };
      let items =
        filter === "excluded"
          ? excluded
            ? [wavelengthSpectrum]
            : []
          : filter === "all"
            ? [wavelengthSpectrum, wavenumberSpectrum]
            : excluded
              ? [wavenumberSpectrum]
              : [wavelengthSpectrum, wavenumberSpectrum];
      if (axisKind) {
        items = items.filter((item) => item.axis_kind === axisKind);
      }
      return {
        items,
        count: filter === "all" ? 2 : items.length,
        limit: limit ?? 2000,
        axis_summary: filter === "all" ? mixedAxisSummary : axisKind === "wavenumber" ? wavenumberAxisSummary : wavelengthAxisSummary
      };
    });
    vi.mocked(api.excludeSpectrum).mockImplementation(async () => {
      excluded = true;
      return { ...baseSpectrum, is_excluded: true };
    });
    vi.mocked(api.restoreSpectrum).mockImplementation(async () => {
      excluded = false;
      return { ...baseSpectrum, is_excluded: false };
    });
    vi.mocked(api.recentExcluded).mockImplementation(async () => (excluded ? [{ ...baseSpectrum, is_excluded: true }] : []));
    vi.mocked(api.createSubsets).mockResolvedValue({
      class_key: baseClass.class_key,
      mode: "count",
      subsets: [{ subset_id: "subset-1", index: 1, count: 1 }]
    });
    vi.mocked(subscribeJob).mockReturnValue(() => undefined);
  });

  it("locks a spectrum and shows its details", async () => {
    const user = userEvent.setup();
    render(<App />);

    await waitFor(() => expect(api.getSpectra).toHaveBeenCalled());
    const curveButton = await screen.findByRole("button", { name: baseSpectrum.file_name });
    await user.click(curveButton);

    expect(await screen.findByText("已锁定")).toBeInTheDocument();
    expect(screen.getByText("棉 60% / 锦纶 40%")).toBeInTheDocument();
    expect(screen.getByText(baseSpectrum.source_path_last_seen)).toBeInTheDocument();
  });

  it("shows axis switching for mixed-axis classes and reloads the selected axis", async () => {
    const user = userEvent.setup();
    render(<App />);

    await waitFor(() => expect(api.getSpectra).toHaveBeenCalled());
    expect(await screen.findByText("波长 (nm) (1)")).toBeInTheDocument();
    expect(screen.getByText("波数 (cm^-1) (1)")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: baseSpectrum.file_name })).toBeInTheDocument();

    await user.click(screen.getByText("波数 (cm^-1) (1)"));
    await waitFor(() =>
      expect(api.getSpectra).toHaveBeenLastCalledWith(
        {
          classKey: baseClass.class_key,
          excluded: "active",
          axisKind: "wavenumber",
          subsetId: undefined,
          limit: 2000
        },
        expect.objectContaining({ signal: expect.any(AbortSignal) })
      )
    );
    expect(await screen.findByRole("button", { name: fourierSpectrum.file_name })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: baseSpectrum.file_name })).not.toBeInTheDocument();
  });

  it("supports quick exclude actions from the chart callback path", async () => {
    const user = userEvent.setup();
    render(<App />);

    await waitFor(() => expect(api.getSpectra).toHaveBeenCalled());
    const quickExcludeButton = await screen.findByRole("button", { name: `quick-exclude-${baseSpectrum.file_name}` });

    await user.click(quickExcludeButton);
    await waitFor(() => expect(api.excludeSpectrum).toHaveBeenCalledWith(baseSpectrum.id));
  });

  it("updates the current preview locally after exclude and restore without refetching detail data", async () => {
    const user = userEvent.setup();
    render(<App />);

    await waitFor(() => expect(api.getSpectra).toHaveBeenCalled());
    const initialDetailCalls = vi.mocked(api.getSpectra).mock.calls.length;

    await user.click(await screen.findByRole("button", { name: `quick-exclude-${baseSpectrum.file_name}` }));
    await waitFor(() => expect(api.excludeSpectrum).toHaveBeenCalledWith(baseSpectrum.id));
    expect(vi.mocked(api.getSpectra).mock.calls.length).toBe(initialDetailCalls);

    const undoButton = (await screen.findAllByRole("button", { name: "撤销最近剔除" })).find((button) => !button.hasAttribute("disabled"));
    expect(undoButton).toBeDefined();
    if (!undoButton) {
      throw new Error("undo button not found");
    }
    await user.click(undoButton);

    await waitFor(() => expect(api.restoreSpectrum).toHaveBeenCalledWith(baseSpectrum.id));
    expect(vi.mocked(api.getSpectra).mock.calls.length).toBe(initialDetailCalls);
  });

  it("shows excluded spectra again when the preview filter switches to only excluded", async () => {
    const user = userEvent.setup();
    render(<App />);

    await waitFor(() => expect(api.getSpectra).toHaveBeenCalled());
    const quickExcludeButton = await screen.findByRole("button", { name: `quick-exclude-${baseSpectrum.file_name}` });

    await user.click(quickExcludeButton);
    await waitFor(() => expect(api.excludeSpectrum).toHaveBeenCalledWith(baseSpectrum.id));
    await waitFor(() => expect(screen.queryByRole("button", { name: baseSpectrum.file_name })).not.toBeInTheDocument());

    await user.click(screen.getByText("仅有效"));
    await user.click(await screen.findByText("仅剔除"));

    await waitFor(() =>
      expect(api.getSpectra).toHaveBeenLastCalledWith(
        {
          classKey: baseClass.class_key,
          excluded: "excluded",
          axisKind: "wavelength",
          subsetId: undefined,
          limit: 2000
        },
        expect.objectContaining({ signal: expect.any(AbortSignal) })
      )
    );
    expect(await screen.findByRole("button", { name: baseSpectrum.file_name })).toBeInTheDocument();
  });

  it("keeps the selected wavenumber axis active after excluding one of multiple visible wavenumber spectra", async () => {
    const user = userEvent.setup();
    vi.mocked(api.getSpectraSummary).mockImplementation(async ({ classKey, excluded: filter }) => {
      if (classKey === secondClass.class_key) {
        return {
          status: "ready",
          progress_message: null,
          total_count: filter === "excluded" ? 0 : 1,
          axis_summary: filter === "excluded" ? [] : wavelengthAxisSummary
        };
      }
      return {
        status: "ready",
        progress_message: null,
        total_count: filter === "all" ? 3 : filter === "excluded" ? 0 : 3,
        axis_summary: [
          { axis_kind: "wavelength", axis_unit: "nm", count: 1 },
          { axis_kind: "wavenumber", axis_unit: "cm^-1", count: filter === "all" ? 2 : filter === "excluded" ? 0 : 2 }
        ]
      };
    });
    vi.mocked(api.getSpectra).mockImplementation(async ({ classKey, excluded: filter, axisKind, limit }) => {
      if (classKey === secondClass.class_key) {
        const items =
          filter === "active" || filter === "all"
            ? [{ ...secondSpectrum, is_excluded: false }]
            : [];
        return { items, count: items.length, limit: limit ?? 2000, axis_summary: wavelengthAxisSummary };
      }

      let items =
        filter === "excluded"
          ? []
          : filter === "all"
            ? [{ ...baseSpectrum, is_excluded: false }, { ...fourierSpectrum, is_excluded: false }, { ...secondFourierSpectrum, is_excluded: false }]
            : [{ ...baseSpectrum, is_excluded: false }, { ...fourierSpectrum, is_excluded: false }, { ...secondFourierSpectrum, is_excluded: false }];
      if (axisKind) {
        items = items.filter((item) => item.axis_kind === axisKind);
      }
      return {
        items,
        count: filter === "all" ? 3 : items.length,
        limit: limit ?? 2000,
        axis_summary:
          axisKind === "wavenumber"
            ? [{ axis_kind: "wavenumber", axis_unit: "cm^-1", count: 2 }]
            : axisKind === "wavelength"
              ? wavelengthAxisSummary
              : [
                  { axis_kind: "wavelength", axis_unit: "nm", count: 1 },
                  { axis_kind: "wavenumber", axis_unit: "cm^-1", count: 2 }
                ]
      };
    });
    vi.mocked(api.excludeSpectrum).mockImplementation(async (id: number) => {
      if (id === fourierSpectrum.id) {
        return { ...fourierSpectrum, is_excluded: true };
      }
      return { ...baseSpectrum, is_excluded: true };
    });
    render(<App />);

    await user.click(await screen.findByText("波数 (cm^-1) (2)"));
    const quickExcludeButton = await screen.findByRole("button", { name: `quick-exclude-${fourierSpectrum.file_name}` });

    await user.click(quickExcludeButton);
    await waitFor(() => expect(api.excludeSpectrum).toHaveBeenCalledWith(fourierSpectrum.id));

    expect(await screen.findByRole("button", { name: secondFourierSpectrum.file_name })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: baseSpectrum.file_name })).not.toBeInTheDocument();
  });

  it("shows an initialization hint instead of a blank page while class stats are building", async () => {
    vi.mocked(api.getClasses).mockResolvedValueOnce({
      items: [],
      meta: { status: "building", progress_message: "正在初始化分类索引" }
    });

    render(<App />);

    expect(await screen.findByText("分类索引初始化中")).toBeInTheDocument();
    expect(screen.getByText(/正在初始化分类索引/)).toBeInTheDocument();
  });

  it("keeps the loading shell visible instead of flashing the empty preview state", async () => {
    vi.mocked(api.getSpectra).mockImplementationOnce(
      async () =>
        new Promise(() => {
          // keep detail loading pending so the preview stays in loading state
        })
    );

    render(<App />);

    await screen.findByText("波数 (cm^-1) (1)");
    expect(screen.queryByText(/没有可预览/)).not.toBeInTheDocument();
  });

  it("short-circuits oversized axis previews before requesting detail data", async () => {
    vi.mocked(api.getSpectraSummary).mockImplementationOnce(async ({ classKey }) => {
      if (classKey !== baseClass.class_key) {
        return {
          status: "ready",
          progress_message: null,
          total_count: 1,
          axis_summary: wavelengthAxisSummary
        };
      }
      return {
        status: "ready",
        progress_message: null,
        total_count: 7000,
        axis_summary: [{ axis_kind: "wavelength", axis_unit: "nm", count: 7000 }]
      };
    });

    render(<App />);

    expect(await screen.findByText("当前轴类型超过渲染上限")).toBeInTheDocument();
    expect(api.getSpectra).not.toHaveBeenCalled();
  });

  it("shows sample id, acquisition time and part metadata for new formats", async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByText("波数 (cm^-1) (1)"));
    const curveButton = await screen.findByRole("button", { name: fourierSpectrum.file_name });
    await user.click(curveButton);

    expect(await screen.findByText("230340227")).toBeInTheDocument();
    expect(screen.getByText("2025-09-09 08:38:55")).toBeInTheDocument();
    expect(screen.getByText("A")).toBeInTheDocument();
  });

  it("locks a clicked spectrum, then excludes it from the detail card and can undo the action", async () => {
    const user = userEvent.setup();
    render(<App />);

    await waitFor(() => expect(api.getSpectra).toHaveBeenCalled());
    const curveButton = await screen.findByRole("button", { name: baseSpectrum.file_name });
    await user.click(curveButton);

    expect(await screen.findByText("已锁定")).toBeInTheDocument();
    expect(api.excludeSpectrum).not.toHaveBeenCalled();

    await user.click(await screen.findByRole("button", { name: "剔除该光谱" }));
    await waitFor(() => expect(api.excludeSpectrum).toHaveBeenCalledWith(baseSpectrum.id));
    expect(await screen.findByText("最近操作")).toBeInTheDocument();

    const undoButton = (await screen.findAllByRole("button", { name: "撤销最近剔除" })).find((button) => !button.hasAttribute("disabled"));
    expect(undoButton).toBeDefined();
    if (!undoButton) {
      throw new Error("undo button not found");
    }
    await user.click(undoButton);

    await waitFor(() => expect(api.restoreSpectrum).toHaveBeenCalledWith(baseSpectrum.id));
  });

  it("creates export jobs with the selected scope", async () => {
    const user = userEvent.setup();
    render(<App />);

    await waitFor(() => expect(api.getSpectra).toHaveBeenCalled());

    await user.click(await screen.findByRole("button", { name: "导出剔除" }));
    await waitFor(() =>
      expect(api.createExportJob).toHaveBeenCalledWith("/workspace/exports", "excluded", [baseClass.class_key])
    );

    await user.click(await screen.findByRole("button", { name: "导出全部" }));
    await waitFor(() =>
      expect(api.createExportJob).toHaveBeenCalledWith("/workspace/exports", "all", [baseClass.class_key])
    );
  });

  it("passes the import label-validation switch to the new job", async () => {
    const user = userEvent.setup();
    render(<App />);

    const validationSwitch = await screen.findByRole("switch", { name: "校验纤维名称和含量总和" });
    expect(validationSwitch).toBeChecked();
    await user.click(validationSwitch);

    const importButton = await screen.findByRole("button", { name: /开始导入/ });
    await waitFor(() => expect(importButton).toBeEnabled());
    await user.click(importButton);

    await waitFor(() => expect(api.createImportJob).toHaveBeenCalledWith("/workspace/imports", false));
  });

  it("creates subsets with the updated count and ratio semantics", async () => {
    const user = userEvent.setup();
    render(<App />);

    await waitFor(() => expect(api.getSpectra).toHaveBeenCalled());

    await user.click(await screen.findByRole("button", { name: "生成子集" }));
    await waitFor(() =>
      expect(api.createSubsets).toHaveBeenCalledWith(baseClass.class_key, { mode: "count", parts: 4 })
    );

    await user.click(screen.getAllByText("按比例切分")[0]);
    const input = screen.getByPlaceholderText("例如 2 表示均分为 2 份");
    await user.clear(input);
    await user.type(input, "2");
    await user.click(await screen.findByRole("button", { name: "生成子集" }));
    await waitFor(() =>
      expect(api.createSubsets).toHaveBeenCalledWith(baseClass.class_key, { mode: "ratio", parts: 2 })
    );
  });

  it("closes subsets and returns to the full preview safely", async () => {
    const user = userEvent.setup();
    render(<App />);

    await waitFor(() => expect(api.getSpectra).toHaveBeenCalled());
    await user.click(await screen.findByRole("button", { name: "生成子集" }));
    expect(await screen.findByRole("tab", { name: "子集1 (1)" })).toBeInTheDocument();

    await user.click(await screen.findByRole("button", { name: "返回全部" }));
    await waitFor(() => expect(screen.queryByRole("tab", { name: "子集1 (1)" })).not.toBeInTheDocument());
  });

  it("clears the previous chart when switching classes", async () => {
    const user = userEvent.setup();
    render(<App />);

    await waitFor(() => expect(api.getSpectra).toHaveBeenCalled());
    expect(await screen.findByRole("button", { name: baseSpectrum.file_name })).toBeInTheDocument();

    await user.click(await screen.findByRole("button", { name: /聚酯纤维/ }));
    expect(await screen.findByRole("button", { name: secondSpectrum.file_name })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: baseSpectrum.file_name })).not.toBeInTheDocument();
  });

  it("shows the unsupported-screen message when the viewport is too small", async () => {
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      writable: true,
      value: 1200
    });

    render(<App />);

    expect(await screen.findByText("当前窗口过窄")).toBeInTheDocument();
    expect(screen.getByText(/1280px 以上/)).toBeInTheDocument();
    expect(api.getSpectra).not.toHaveBeenCalled();
  });
});
