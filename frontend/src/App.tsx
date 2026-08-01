import {
  App as AntdApp,
  Alert,
  Badge,
  Button,
  Card,
  ConfigProvider,
  Descriptions,
  Divider,
  Empty,
  Input,
  InputNumber,
  Layout,
  List,
  Progress,
  Segmented,
  Select,
  Skeleton,
  Space,
  Spin,
  Statistic,
  Switch,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  message,
  notification
} from "antd";
import {
  BarChartOutlined,
  CloudDownloadOutlined,
  CloudUploadOutlined,
  FolderOpenOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  ReloadOutlined,
  UndoOutlined
} from "@ant-design/icons";
import { lazy, startTransition, Suspense, useDeferredValue, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, subscribeJob } from "./api";
import { formatSpectrumLabels, getAxisDisplayLabel } from "./chartInteraction";
import type { AxisKind, AxisSummary, FsEntry, JobItem, LoadingMeta, SpectrumClass, SpectrumItem, SubsetSummary } from "./types";
import "./styles.css";

const SpectrumChart = lazy(() =>
  import("./SpectrumChart").then((module) => ({ default: module.SpectrumChart }))
);

const { Content, Sider } = Layout;
const { Paragraph, Text, Title } = Typography;

type PathChooserProps = {
  kind: "import" | "export";
  currentPath: string | null;
  onSelect: (path: string) => void;
  title: string;
  actionLabel: string;
  actionIcon: ReactNode;
  actionDisabled?: boolean;
  onAction: () => Promise<void>;
  children?: ReactNode;
};

type ExportScope = "active" | "excluded" | "all";

type PreviewLoadState = {
  percent: number;
  message: string;
};

type PreviewState = "idle" | "loading-summary" | "loading-detail" | "ready" | "empty" | "over-limit" | "error";

type SpectraResponse = Awaited<ReturnType<typeof api.getSpectra>>;

const PREVIEW_LOADING_DELAY_MS = 200;
const SUMMARY_CACHE_MAX_ENTRIES = 128;
const DETAIL_CACHE_MAX_ENTRIES = 24;

const EXPORT_SCOPE_LABELS: Record<ExportScope, string> = {
  active: "未剔除",
  excluded: "已剔除",
  all: "全部"
};

const AXIS_KIND_PRIORITY: AxisKind[] = ["wavelength", "wavenumber"];

function sortAxisSummary(summary: AxisSummary[]): AxisSummary[] {
  return [...summary].sort((left, right) => {
    const leftPriority = AXIS_KIND_PRIORITY.indexOf(left.axis_kind);
    const rightPriority = AXIS_KIND_PRIORITY.indexOf(right.axis_kind);
    return (leftPriority === -1 ? 99 : leftPriority) - (rightPriority === -1 ? 99 : rightPriority);
  });
}

function pickPreferredAxisKind(summary: AxisSummary[]): AxisKind | undefined {
  return sortAxisSummary(summary)[0]?.axis_kind;
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function getSummaryCacheKey(classKey: string, excluded: "active" | "excluded" | "all", subsetId?: string) {
  return `${classKey}::${excluded}::${subsetId ?? "__all__"}`;
}

function getDetailCacheKey(
  classKey: string,
  excluded: "active" | "excluded" | "all",
  subsetId: string | undefined,
  axisKind: AxisKind
) {
  return `${getSummaryCacheKey(classKey, excluded, subsetId)}::${axisKind}`;
}

function getLruValue<K, V>(cache: Map<K, V>, key: K): V | undefined {
  const value = cache.get(key);
  if (value !== undefined) {
    cache.delete(key);
    cache.set(key, value);
  }
  return value;
}

function setLruValue<K, V>(cache: Map<K, V>, key: K, value: V, maxEntries: number) {
  cache.delete(key);
  cache.set(key, value);
  while (cache.size > maxEntries) {
    const oldestKey = cache.keys().next().value as K | undefined;
    if (oldestKey === undefined) {
      break;
    }
    cache.delete(oldestKey);
  }
}

function useViewportWidth() {
  const [width, setWidth] = useState(() => window.innerWidth);

  useEffect(() => {
    const handleResize = () => setWidth(window.innerWidth);
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, []);

  return width;
}

function PathChooserCard({
  kind,
  currentPath,
  onSelect,
  title,
  actionLabel,
  actionIcon,
  actionDisabled = false,
  onAction,
  children
}: PathChooserProps) {
  const [roots, setRoots] = useState<string[]>([]);
  const [entries, setEntries] = useState<FsEntry[]>([]);
  const [parentPath, setParentPath] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [browseError, setBrowseError] = useState<string | null>(null);

  useEffect(() => {
    api
      .getRoots()
      .then((data) => {
        const nextRoots = kind === "import" ? data.import_roots : data.export_roots;
        setRoots(nextRoots);
        if (!currentPath && nextRoots[0]) {
          onSelect(nextRoots[0]);
        }
      })
      .catch((error) => setBrowseError(String(error)));
  }, [currentPath, kind, onSelect]);

  useEffect(() => {
    if (!currentPath) {
      setEntries([]);
      setParentPath(null);
      return;
    }
    setLoading(true);
    api
      .browse(kind, currentPath)
      .then((data) => {
        setEntries(data.entries.filter((entry) => entry.is_dir));
        setParentPath(data.parent_path ?? null);
        setBrowseError(null);
        setLoading(false);
      })
      .catch((error) => {
        setLoading(false);
        setBrowseError(String(error));
      });
  }, [currentPath, kind]);

  const selectedRoot = useMemo(() => {
    if (!currentPath) {
      return undefined;
    }
    return roots.find((root) => currentPath.startsWith(root)) ?? roots[0];
  }, [currentPath, roots]);

  return (
    <Card
      size="small"
      title={title}
      extra={
        <Button type="primary" icon={actionIcon} disabled={actionDisabled || !currentPath} onClick={() => void onAction()}>
          {actionLabel}
        </Button>
      }
    >
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        <Select
          value={selectedRoot}
          placeholder="选择根目录"
          options={roots.map((root) => ({ value: root, label: root }))}
          onChange={(value) => onSelect(value)}
        />
        <div className="path-current-card">
          <Text type="secondary">当前路径</Text>
          <Paragraph className="path-current-text" ellipsis={{ rows: 2, expandable: true, symbol: "展开" }}>
            {currentPath ?? "未选择目录"}
          </Paragraph>
        </div>
        {children}
        {browseError && <Alert type="error" showIcon message="目录读取失败" description={browseError} />}
        <div className="path-actions">
          {parentPath && (
            <Button icon={<UndoOutlined />} onClick={() => onSelect(parentPath)}>
              返回上一级
            </Button>
          )}
          {entries.map((entry) => (
            <Button key={entry.path} icon={<FolderOpenOutlined />} onClick={() => onSelect(entry.path)}>
              {entry.name}
            </Button>
          ))}
          {!loading && entries.length === 0 && <Text type="secondary">当前目录下没有可进入的子目录</Text>}
        </div>
        {loading && <Spin size="small" />}
      </Space>
    </Card>
  );
}

function DetailCard(props: {
  spectrum: SpectrumItem | null;
  modeLabel: string | null;
  compact?: boolean;
  onExclude: (spectrum: SpectrumItem) => Promise<void>;
  onRestore: (spectrum: SpectrumItem) => Promise<void>;
  onClearLock: () => void;
}) {
  const { spectrum, modeLabel, compact = false, onExclude, onRestore, onClearLock } = props;
  const metadataRows = spectrum
    ? [
        spectrum.metadata.sample_id
          ? { key: "sample_id", label: "样品编号", value: String(spectrum.metadata.sample_id) }
          : null,
        spectrum.metadata.acquisition_date || spectrum.metadata.acquisition_time
          ? {
              key: "acquisition",
              label: "采集时间",
              value: [spectrum.metadata.acquisition_date, spectrum.metadata.acquisition_time].filter(Boolean).join(" ")
            }
          : null,
        spectrum.metadata.part_name
          ? { key: "part_name", label: "部位", value: String(spectrum.metadata.part_name) }
          : null,
        spectrum.metadata.device_serial
          ? { key: "device_serial", label: "设备序列号", value: String(spectrum.metadata.device_serial) }
          : null,
        spectrum.metadata.scan_config
          ? { key: "scan_config", label: "扫描配置", value: String(spectrum.metadata.scan_config) }
          : null,
        { key: "source_path", label: "来源路径", value: spectrum.source_path_last_seen }
      ].filter((item): item is { key: string; label: string; value: string } => item !== null)
    : [];
  return (
    <Card
      className="detail-card"
      size={compact ? "small" : "default"}
      title="当前光谱"
      extra={
        modeLabel ? (
          <Tag color={modeLabel === "最近操作" ? "processing" : "gold"}>{modeLabel}</Tag>
        ) : (
          <Tag>未选中</Tag>
        )
      }
    >
      {!spectrum ? (
        <Empty description="单击图谱锁定后，可在这里查看详情并执行剔除" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      ) : (
        <div className="detail-card-scroll">
          <Space direction="vertical" size="middle" style={{ width: "100%" }}>
            <div>
              <Tooltip title={spectrum.file_name}>
                <Paragraph className="detail-file-name" ellipsis={{ rows: 2 }}>
                  {spectrum.file_name}
                </Paragraph>
              </Tooltip>
              <Space wrap>
                <Tag color={spectrum.is_excluded ? "default" : "success"}>{spectrum.is_excluded ? "已剔除" : "有效"}</Tag>
                <Tag>{spectrum.class_display_name}</Tag>
                <Tag>{spectrum.point_count} 点</Tag>
              </Space>
            </div>
            <Descriptions column={1} size="small" bordered className={compact ? "detail-descriptions-compact" : undefined}>
              <Descriptions.Item label="成分">
                <Tooltip title={formatSpectrumLabels(spectrum.labels) || "无标签"}>
                  <span className="detail-value-text">{formatSpectrumLabels(spectrum.labels) || "无标签"}</span>
                </Tooltip>
              </Descriptions.Item>
              {metadataRows.map((row) => (
                <Descriptions.Item key={row.key} label={row.label}>
                  <Tooltip title={row.value}>
                    <span className="detail-value-text">{row.value}</span>
                  </Tooltip>
                </Descriptions.Item>
              ))}
            </Descriptions>
            <Space wrap>
              {modeLabel === "已锁定" && (
                <Button onClick={onClearLock}>
                  取消锁定
                </Button>
              )}
              {spectrum.is_excluded ? (
                <Button icon={<UndoOutlined />} onClick={() => void onRestore(spectrum)}>
                  恢复该光谱
                </Button>
              ) : (
                <Button danger type="primary" onClick={() => void onExclude(spectrum)}>
                  剔除该光谱
                </Button>
              )}
            </Space>
          </Space>
        </div>
      )}
    </Card>
  );
}

function Workspace() {
  const [messageApi, messageContextHolder] = message.useMessage();
  const [notificationApi, notificationContextHolder] = notification.useNotification();
  const viewportWidth = useViewportWidth();
  const isScreenTooSmall = viewportWidth < 1280;

  const [classes, setClasses] = useState<SpectrumClass[]>([]);
  const [classesMeta, setClassesMeta] = useState<LoadingMeta>({ status: "ready", progress_message: null });
  const [loadingClasses, setLoadingClasses] = useState(true);
  const [loadingJobs, setLoadingJobs] = useState(true);
  const [loadingRecentExcluded, setLoadingRecentExcluded] = useState(true);
  const [classSort, setClassSort] = useState<"count" | "component_count" | "name">("count");
  const [classSearch, setClassSearch] = useState("");
  const deferredClassSearch = useDeferredValue(classSearch);
  const [selectedClass, setSelectedClass] = useState<SpectrumClass | null>(null);
  const [spectra, setSpectra] = useState<SpectrumItem[]>([]);
  const [excludedFilter, setExcludedFilter] = useState<"active" | "excluded" | "all">("active");
  const [jobs, setJobs] = useState<JobItem[]>([]);
  const [recentExcluded, setRecentExcluded] = useState<SpectrumItem[]>([]);
  const [importPath, setImportPath] = useState<string | null>(null);
  const [validateImportLabels, setValidateImportLabels] = useState(true);
  const [exportPath, setExportPath] = useState<string | null>(null);
  const [subsetMode, setSubsetMode] = useState<"count" | "ratio">("count");
  const [subsetInput, setSubsetInput] = useState<string>("4");
  const [subsets, setSubsets] = useState<SubsetSummary[]>([]);
  const [activeSubsetId, setActiveSubsetId] = useState<string | undefined>();
  const [axisSummary, setAxisSummary] = useState<AxisSummary[]>([]);
  const [selectedAxisKind, setSelectedAxisKind] = useState<AxisKind | undefined>();
  const [spectraTotal, setSpectraTotal] = useState(0);
  const [previewState, setPreviewState] = useState<PreviewState>("idle");
  const [previewLoad, setPreviewLoad] = useState<PreviewLoadState>({ percent: 0, message: "" });
  const [loadingIndicatorVisible, setLoadingIndicatorVisible] = useState(false);
  const [previewLimitMessage, setPreviewLimitMessage] = useState<string | null>(null);
  const [errorText, setErrorText] = useState<string | null>(null);
  const [exportSelectedOnly, setExportSelectedOnly] = useState(true);
  const [lockedSpectrum, setLockedSpectrum] = useState<SpectrumItem | null>(null);
  const [pendingUndoSpectrum, setPendingUndoSpectrum] = useState<SpectrumItem | null>(null);
  const [chartResetToken, setChartResetToken] = useState(0);
  const [leftPanelVisible, setLeftPanelVisible] = useState(true);
  const [rightPanelVisible, setRightPanelVisible] = useState(true);
  const subscriptionsRef = useRef<Map<number, () => void>>(new Map());
  const classesAbortRef = useRef<AbortController | null>(null);
  const summaryAbortRef = useRef<AbortController | null>(null);
  const detailAbortRef = useRef<AbortController | null>(null);
  const pollTimeoutRef = useRef<number | null>(null);
  const loadingIndicatorTimerRef = useRef<number | null>(null);
  const previewTokenRef = useRef(0);
  const summaryCacheRef = useRef<Map<string, LoadingMeta & { total_count: number; axis_summary: AxisSummary[] }>>(new Map());
  const detailCacheRef = useRef<Map<string, SpectraResponse>>(new Map());

  const filteredClasses = useMemo(() => {
    const keyword = deferredClassSearch.trim();
    return classes.filter((item) => !keyword || item.class_display_name.includes(keyword));
  }, [classes, deferredClassSearch]);
  const previewSpectra = useMemo(() => spectra, [spectra]);
  const selectedClassKey = selectedClass?.class_key;
  const selectedClassLabel = selectedClass?.class_display_name ?? "未选择分类";
  const selectedAxisSummary = axisSummary.find((item) => item.axis_kind === selectedAxisKind);
  const selectedAxisLabel = selectedAxisSummary
    ? getAxisDisplayLabel(selectedAxisSummary.axis_kind, selectedAxisSummary.axis_unit)
    : null;
  const currentAxisCount = selectedAxisSummary?.count ?? 0;
  const canRender = currentAxisCount <= 2000;
  const isPreviewLoading = previewState === "loading-summary" || previewState === "loading-detail";
  const chartDensityMode = previewSpectra.length > 800 ? "dense" : previewSpectra.length > 200 ? "medium" : "full";
  const detailSpectrum = lockedSpectrum ?? pendingUndoSpectrum;
  const detailModeLabel = lockedSpectrum ? "已锁定" : pendingUndoSpectrum ? "最近操作" : null;

  function cancelClassRequest() {
    classesAbortRef.current?.abort();
    classesAbortRef.current = null;
  }

  function cancelPreviewRequests() {
    summaryAbortRef.current?.abort();
    summaryAbortRef.current = null;
    detailAbortRef.current?.abort();
    detailAbortRef.current = null;
  }

  function clearPreviewLoadingIndicator() {
    if (loadingIndicatorTimerRef.current !== null) {
      window.clearTimeout(loadingIndicatorTimerRef.current);
      loadingIndicatorTimerRef.current = null;
    }
    setLoadingIndicatorVisible(false);
  }

  function beginPreviewLoading(nextState: Extract<PreviewState, "loading-summary" | "loading-detail">, percent: number, message: string) {
    clearPreviewLoadingIndicator();
    setPreviewState(nextState);
    setPreviewLoad({ percent, message });
    loadingIndicatorTimerRef.current = window.setTimeout(() => {
      setLoadingIndicatorVisible(true);
    }, PREVIEW_LOADING_DELAY_MS);
  }

  function updatePreviewLoading(nextState: Extract<PreviewState, "loading-summary" | "loading-detail">, percent: number, message: string) {
    setPreviewState(nextState);
    setPreviewLoad({ percent, message });
  }

  function finishPreviewLoading(nextState: Exclude<PreviewState, "loading-summary" | "loading-detail">) {
    clearPreviewLoadingIndicator();
    setPreviewState(nextState);
    setPreviewLoad({ percent: 0, message: "" });
  }

  function applySummaryPayload(
    data: LoadingMeta & { total_count: number; axis_summary: AxisSummary[] },
    preferredAxisKind?: AxisKind
  ) {
    const nextAxisSummary = sortAxisSummary(data.axis_summary);
    const nextSelectedAxisKind =
      preferredAxisKind && nextAxisSummary.some((item) => item.axis_kind === preferredAxisKind)
        ? preferredAxisKind
        : pickPreferredAxisKind(nextAxisSummary);
    startTransition(() => {
      setAxisSummary(nextAxisSummary);
      setSelectedAxisKind(nextSelectedAxisKind);
      setSpectraTotal(data.total_count);
    });
    return { nextAxisSummary, nextSelectedAxisKind };
  }

  function invalidatePreviewCachesForClass(classKey: string, keepKeys?: { summaryKey?: string; detailKey?: string }) {
    for (const key of Array.from(summaryCacheRef.current.keys())) {
      if (key.startsWith(`${classKey}::`) && key !== keepKeys?.summaryKey) {
        summaryCacheRef.current.delete(key);
      }
    }
    for (const key of Array.from(detailCacheRef.current.keys())) {
      if (key.startsWith(`${classKey}::`) && key !== keepKeys?.detailKey) {
        detailCacheRef.current.delete(key);
      }
    }
  }

  function patchCurrentSummaryCache(nextTotal: number, nextAxisSummary: AxisSummary[]) {
    if (!selectedClass) {
      return;
    }
    setLruValue(
      summaryCacheRef.current,
      getSummaryCacheKey(selectedClass.class_key, excludedFilter, activeSubsetId),
      {
        status: "ready",
        progress_message: null,
        total_count: nextTotal,
        axis_summary: nextAxisSummary
      },
      SUMMARY_CACHE_MAX_ENTRIES
    );
  }

  function patchCurrentDetailCache(nextItems: SpectrumItem[], nextTotal: number, nextAxisSummary: AxisSummary[]) {
    if (!selectedClass || !selectedAxisKind) {
      return;
    }
    const detailKey = getDetailCacheKey(selectedClass.class_key, excludedFilter, activeSubsetId, selectedAxisKind);
    if (!nextAxisSummary.some((item) => item.axis_kind === selectedAxisKind)) {
      detailCacheRef.current.delete(detailKey);
      return;
    }
    const currentCache = detailCacheRef.current.get(detailKey);
    setLruValue(
      detailCacheRef.current,
      detailKey,
      {
        ...currentCache,
        items: nextItems,
        count: nextTotal,
        limit: 2000,
        axis_summary: nextAxisSummary
      },
      DETAIL_CACHE_MAX_ENTRIES
    );
  }

  function applyPreviewTargetReset(resetAxisSelection: boolean) {
    cancelPreviewRequests();
    clearPreviewLoadingIndicator();
    previewTokenRef.current += 1;
    setSpectra([]);
    setSpectraTotal(0);
    setPreviewLimitMessage(null);
    setLockedSpectrum(null);
    setPreviewState(resetAxisSelection ? "loading-summary" : "loading-detail");
    if (resetAxisSelection) {
      setAxisSummary([]);
      setSelectedAxisKind(undefined);
    }
  }

  function clearPreviewState(resetAxisSelection: boolean) {
    cancelPreviewRequests();
    clearPreviewLoadingIndicator();
    previewTokenRef.current += 1;
    setSpectra([]);
    setSpectraTotal(0);
    setPreviewLoad({ percent: 0, message: "" });
    setPreviewLimitMessage(null);
    setLockedSpectrum(null);
    setPreviewState("idle");
    if (resetAxisSelection) {
      setAxisSummary([]);
      setSelectedAxisKind(undefined);
    }
  }

  async function refreshClasses(options?: { silent?: boolean }) {
    cancelClassRequest();
    const controller = new AbortController();
    classesAbortRef.current = controller;
    if (!options?.silent || classes.length === 0) {
      setLoadingClasses(true);
    }
    try {
      const data = await api.getClasses(classSort, { signal: controller.signal });
      startTransition(() => {
        setClasses(data.items);
        setClassesMeta(data.meta);
        setSelectedClass((current) => {
          if (current) {
            return data.items.find((item) => item.class_key === current.class_key) ?? data.items[0] ?? null;
          }
          return data.items[0] ?? null;
        });
      });
    } catch (error) {
      if (!isAbortError(error)) {
        setErrorText(String(error));
      }
    } finally {
      if (classesAbortRef.current === controller) {
        classesAbortRef.current = null;
        setLoadingClasses(false);
      }
    }
  }

  async function refreshExcluded() {
    setLoadingRecentExcluded(true);
    try {
      const items = await api.recentExcluded();
      startTransition(() => setRecentExcluded(items));
    } catch (error) {
      setErrorText(String(error));
    } finally {
      setLoadingRecentExcluded(false);
    }
  }

  async function refreshJobs() {
    setLoadingJobs(true);
    try {
      const items = await api.listJobs();
      startTransition(() => setJobs(items));
    } catch (error) {
      setErrorText(String(error));
    } finally {
      setLoadingJobs(false);
    }
  }

  async function loadPreviewSummary(
    targetClass: SpectrumClass,
    targetFilter: "active" | "excluded" | "all",
    subsetId: string | undefined,
    preferredAxisKind?: AxisKind,
    options?: { bypassCache?: boolean; showLoading?: boolean }
  ) {
    cancelPreviewRequests();
    const summaryKey = getSummaryCacheKey(targetClass.class_key, targetFilter, subsetId);
    const cached = !options?.bypassCache ? getLruValue(summaryCacheRef.current, summaryKey) : undefined;
    if (cached) {
      const { nextAxisSummary } = applySummaryPayload(cached, preferredAxisKind);
      setPreviewLimitMessage(null);
      if (nextAxisSummary.length === 0) {
        finishPreviewLoading("empty");
      }
      return;
    }
    const controller = new AbortController();
    summaryAbortRef.current = controller;
    const previewToken = previewTokenRef.current;
    if (options?.showLoading !== false) {
      beginPreviewLoading("loading-summary", 20, "正在查询分类摘要...");
    }
    setPreviewLimitMessage(null);
    try {
      const data = await api.getSpectraSummary(
        {
          classKey: targetClass.class_key,
          excluded: targetFilter,
          subsetId,
        },
        { signal: controller.signal }
      );
      if (previewToken !== previewTokenRef.current) {
        return;
      }
      setLruValue(summaryCacheRef.current, summaryKey, data, SUMMARY_CACHE_MAX_ENTRIES);
      const { nextAxisSummary } = applySummaryPayload(data, preferredAxisKind);

      if (nextAxisSummary.length === 0) {
        finishPreviewLoading("empty");
        return;
      }
      updatePreviewLoading("loading-detail", 50, data.progress_message || "摘要分析完成，正在准备光谱明细...");
    } catch (error) {
      if (!isAbortError(error)) {
        finishPreviewLoading("error");
        setErrorText(String(error));
      }
    } finally {
      if (summaryAbortRef.current === controller) {
        summaryAbortRef.current = null;
      }
    }
  }

  async function loadPreviewDetail(
    targetClass: SpectrumClass,
    targetFilter: "active" | "excluded" | "all",
    subsetId: string | undefined,
    axisKind: AxisKind,
    options?: { bypassCache?: boolean; showLoading?: boolean }
  ) {
    detailAbortRef.current?.abort();
    const detailKey = getDetailCacheKey(targetClass.class_key, targetFilter, subsetId, axisKind);
    const cached = !options?.bypassCache ? getLruValue(detailCacheRef.current, detailKey) : undefined;
    if (cached) {
      startTransition(() => setSpectra(cached.items));
      setPreviewLimitMessage(
        cached.truncated_by_point_budget
          ? `为控制浏览器内存，本次按点数预算加载了 ${cached.items.length} 条（${cached.returned_point_count ?? 0} 个数据点）；请生成更小的子集以查看其余光谱。`
          : null
      );
      finishPreviewLoading(cached.items.length > 0 ? "ready" : "empty");
      return;
    }
    const controller = new AbortController();
    detailAbortRef.current = controller;
    if (options?.showLoading !== false) {
      beginPreviewLoading("loading-detail", 60, "正在读取光谱明细...");
    }
    try {
      const data = await api.getSpectra(
        {
          classKey: targetClass.class_key,
          excluded: targetFilter,
          axisKind,
          subsetId,
          limit: 2000,
        },
        { signal: controller.signal }
      );
      setLruValue(detailCacheRef.current, detailKey, data, DETAIL_CACHE_MAX_ENTRIES);
      startTransition(() => setSpectra(data.items));
      setPreviewLimitMessage(
        data.truncated_by_point_budget
          ? `为控制浏览器内存，本次按点数预算加载了 ${data.items.length} 条（${data.returned_point_count ?? 0} 个数据点）；请生成更小的子集以查看其余光谱。`
          : null
      );
      finishPreviewLoading(data.items.length > 0 ? "ready" : "empty");
    } catch (error) {
      if (!isAbortError(error)) {
        finishPreviewLoading("error");
        setErrorText(String(error));
      }
    } finally {
      if (detailAbortRef.current === controller) {
        detailAbortRef.current = null;
      }
    }
  }

  async function reloadPreview(preferredAxisKind?: AxisKind, options?: { bypassCache?: boolean; showLoading?: boolean }) {
    if (!selectedClass || isScreenTooSmall) {
      return;
    }
    applyPreviewTargetReset(true);
    await loadPreviewSummary(selectedClass, excludedFilter, activeSubsetId, preferredAxisKind, options);
  }

  function trackJob(job: JobItem) {
    setJobs((current) => [job, ...current.filter((item) => item.id !== job.id)].slice(0, 20));
    const unsubscribe = subscribeJob(job.id, (next) => {
      setJobs((current) => [next, ...current.filter((item) => item.id !== next.id)].slice(0, 20));
      if (next.status === "completed" || next.status === "failed") {
        subscriptionsRef.current.get(job.id)?.();
        subscriptionsRef.current.delete(job.id);
      }
      if (next.status === "completed" && next.type === "import") {
        void refreshClasses({ silent: true });
        void refreshExcluded();
      }
    });
    subscriptionsRef.current.set(job.id, unsubscribe);
  }

  async function startImport() {
    try {
      if (!importPath) {
        messageApi.warning("请先选择导入目录");
        return;
      }
      const job = await api.createImportJob(importPath, validateImportLabels);
      trackJob(job);
      messageApi.success("导入任务已创建");
    } catch (error) {
      const messageText = String(error);
      setErrorText(messageText);
      messageApi.error(`导入任务创建失败：${messageText}`);
    }
  }

  async function startExport(scope: ExportScope) {
    try {
      if (!exportPath) {
        messageApi.warning("请先选择导出目录");
        return;
      }
      if (exportSelectedOnly && !selectedClass) {
        messageApi.warning("请先选择分类，或关闭“仅导出当前选中分类”");
        return;
      }
      const classKeys = exportSelectedOnly && selectedClass ? [selectedClass.class_key] : [];
      const job = await api.createExportJob(exportPath, scope, classKeys);
      trackJob(job);
      messageApi.success(`${EXPORT_SCOPE_LABELS[scope]}光谱导出任务已创建`);
    } catch (error) {
      const messageText = String(error);
      setErrorText(messageText);
      messageApi.error(`导出任务创建失败：${messageText}`);
    }
  }

  function patchClassCountsLocally(classKey: string, activeDelta: number, excludedDelta: number) {
    startTransition(() => {
      setClasses((current) =>
        current.map((item) =>
          item.class_key === classKey
            ? {
                ...item,
                active_count: Math.max(0, item.active_count + activeDelta),
                excluded_count: Math.max(0, item.excluded_count + excludedDelta)
              }
            : item
        )
      );
      setSelectedClass((current) =>
        current && current.class_key === classKey
          ? {
              ...current,
              active_count: Math.max(0, current.active_count + activeDelta),
              excluded_count: Math.max(0, current.excluded_count + excludedDelta)
            }
          : current
      );
    });
  }

  function patchAxisSummaryForCurrentView(currentSummary: AxisSummary[], spectrum: SpectrumItem, delta: number) {
    if (excludedFilter === "all") {
      return currentSummary;
    }
    const nextMap = new Map(currentSummary.map((item) => [`${item.axis_kind}::${item.axis_unit}`, { ...item }]));
    const key = `${spectrum.axis_kind}::${spectrum.axis_unit}`;
    const currentItem = nextMap.get(key);
    if (currentItem) {
      currentItem.count += delta;
      if (currentItem.count <= 0) {
        if (selectedAxisKind === currentItem.axis_kind) {
          currentItem.count = 0;
          nextMap.set(key, currentItem);
        } else {
          nextMap.delete(key);
        }
      } else {
        nextMap.set(key, currentItem);
      }
    } else if (delta > 0) {
      nextMap.set(key, { axis_kind: spectrum.axis_kind as AxisKind, axis_unit: spectrum.axis_unit, count: delta });
    }
    return sortAxisSummary(
      Array.from(nextMap.values()).filter(
        (item) => item.count > 0 || item.axis_kind === selectedAxisKind
      )
    );
  }

  function sortSpectraByFileName(items: SpectrumItem[]) {
    return [...items].sort((left, right) => left.file_name.localeCompare(right.file_name, "zh-Hans-CN"));
  }

  async function restoreSpectrumItem(spectrum: SpectrumItem) {
    try {
      const restored = await api.restoreSpectrum(spectrum.id);
      setPendingUndoSpectrum(null);
      setLockedSpectrum((current) => (current?.id === restored.id ? restored : current));
      setRecentExcluded((current) => current.filter((item) => item.id !== restored.id));
      patchClassCountsLocally(restored.class_key, 1, -1);

      let nextSpectra = spectra;
      let nextAxisSummary = axisSummary;
      let nextTotal = spectraTotal;
      let nextPreviewState = previewState;
      const affectsCurrentClass = selectedClass?.class_key === restored.class_key;
      const isVisibleInCurrentView = spectra.some((item) => item.id === restored.id);
      const canInsertIntoCurrentActiveView =
        affectsCurrentClass &&
        excludedFilter === "active" &&
        !activeSubsetId &&
        (!selectedAxisKind || selectedAxisKind === restored.axis_kind);

      if (affectsCurrentClass) {
        if (excludedFilter === "excluded" && isVisibleInCurrentView) {
          nextSpectra = spectra.filter((item) => item.id !== restored.id);
          nextAxisSummary = patchAxisSummaryForCurrentView(axisSummary, restored, -1);
          nextTotal = Math.max(0, spectraTotal - 1);
        } else if (excludedFilter === "all" && isVisibleInCurrentView) {
          nextSpectra = spectra.map((item) => (item.id === restored.id ? restored : item));
        } else if (canInsertIntoCurrentActiveView) {
          nextSpectra = sortSpectraByFileName([...spectra.filter((item) => item.id !== restored.id), restored]);
          nextAxisSummary = patchAxisSummaryForCurrentView(axisSummary, restored, 1);
          nextTotal = spectraTotal + 1;
        }

        if (nextPreviewState !== "over-limit" && nextPreviewState !== "error") {
          const hasAlternativeAxis =
            nextSpectra.length === 0 &&
            nextAxisSummary.length > 0 &&
            (!!selectedAxisKind && !nextAxisSummary.some((item) => item.axis_kind === selectedAxisKind));
          nextPreviewState = hasAlternativeAxis ? "loading-detail" : nextSpectra.length > 0 ? "ready" : "empty";
        }
        startTransition(() => {
          setSpectra(nextSpectra);
          setAxisSummary(nextAxisSummary);
          setSpectraTotal(nextTotal);
          setPreviewState(nextPreviewState);
        });
        patchCurrentSummaryCache(nextTotal, nextAxisSummary);
        patchCurrentDetailCache(nextSpectra, nextTotal, nextAxisSummary);
        invalidatePreviewCachesForClass(restored.class_key, {
          summaryKey: getSummaryCacheKey(restored.class_key, excludedFilter, activeSubsetId),
          detailKey:
            selectedAxisKind && selectedClass
              ? getDetailCacheKey(restored.class_key, excludedFilter, activeSubsetId, selectedAxisKind)
              : undefined
        });
      } else {
        invalidatePreviewCachesForClass(restored.class_key);
      }
      messageApi.success(`已恢复 ${restored.file_name}`);
    } catch (error) {
      const messageText = String(error);
      setErrorText(messageText);
      messageApi.error(`恢复失败：${messageText}`);
    }
  }

  async function handleExclude(spectrum: SpectrumItem) {
    try {
      if (spectrum.is_excluded) {
        return;
      }
      const updated = await api.excludeSpectrum(spectrum.id);
      setLockedSpectrum(null);
      setPendingUndoSpectrum(updated);
      setRecentExcluded((current) => [updated, ...current.filter((item) => item.id !== updated.id)].slice(0, 50));
      patchClassCountsLocally(updated.class_key, -1, 1);

      let nextSpectra = spectra;
      let nextAxisSummary = axisSummary;
      let nextTotal = spectraTotal;
      let nextPreviewState = previewState;
      const affectsCurrentClass = selectedClass?.class_key === updated.class_key;
      if (affectsCurrentClass) {
        if (excludedFilter === "active") {
          nextSpectra = spectra.filter((item) => item.id !== updated.id);
          nextAxisSummary = patchAxisSummaryForCurrentView(axisSummary, updated, -1);
          nextTotal = Math.max(0, spectraTotal - 1);
        } else if (excludedFilter === "all" || excludedFilter === "excluded") {
          nextSpectra = spectra.map((item) => (item.id === updated.id ? updated : item));
        }

        if (nextPreviewState !== "over-limit" && nextPreviewState !== "error") {
          const hasAlternativeAxis =
            nextSpectra.length === 0 &&
            nextAxisSummary.length > 0 &&
            (!!selectedAxisKind && !nextAxisSummary.some((item) => item.axis_kind === selectedAxisKind));
          nextPreviewState = hasAlternativeAxis ? "loading-detail" : nextSpectra.length > 0 ? "ready" : "empty";
        }
        startTransition(() => {
          setSpectra(nextSpectra);
          setAxisSummary(nextAxisSummary);
          setSpectraTotal(nextTotal);
          setPreviewState(nextPreviewState);
        });
        patchCurrentSummaryCache(nextTotal, nextAxisSummary);
        patchCurrentDetailCache(nextSpectra, nextTotal, nextAxisSummary);
        invalidatePreviewCachesForClass(updated.class_key, {
          summaryKey: getSummaryCacheKey(updated.class_key, excludedFilter, activeSubsetId),
          detailKey:
            selectedAxisKind && selectedClass
              ? getDetailCacheKey(updated.class_key, excludedFilter, activeSubsetId, selectedAxisKind)
              : undefined
        });
      } else {
        invalidatePreviewCachesForClass(updated.class_key);
      }

      const notificationKey = `exclude-${updated.id}`;
      notificationApi.open({
        key: notificationKey,
        message: `已剔除 ${updated.file_name}`,
        description: formatSpectrumLabels(updated.labels) || "无标签",
        duration: 6,
        actions: (
          <Button
            size="small"
            type="primary"
            onClick={() => {
              notificationApi.destroy(notificationKey);
              void restoreSpectrumItem(updated);
            }}
          >
            撤销
          </Button>
        )
      });
    } catch (error) {
      const messageText = String(error);
      setErrorText(messageText);
      messageApi.error(`剔除失败：${messageText}`);
    }
  }

  async function handleUndo() {
    if (!pendingUndoSpectrum) {
      return;
    }
    await restoreSpectrumItem(pendingUndoSpectrum);
  }

  function clearLockedSpectrum() {
    setLockedSpectrum(null);
  }

  const [creatingSubsets, setCreatingSubsets] = useState(false);

  async function createSubsets() {
    try {
      if (!selectedClass) {
        messageApi.warning("请先选择分类");
        return;
      }
      setCreatingSubsets(true);
      const parts = Math.max(1, Number(subsetInput) || 1);
      const payload = { mode: subsetMode, parts } as const;
      const result = await api.createSubsets(selectedClass.class_key, payload);
      setSubsets(result.subsets);
      setActiveSubsetId(undefined);
      messageApi.success(`已生成 ${result.subsets.length} 个子集`);
    } catch (error) {
      const messageText = String(error);
      setErrorText(messageText);
      messageApi.error(`子集切分失败：${messageText}`);
    } finally {
      setCreatingSubsets(false);
    }
  }

  function closeSubsets() {
    const shouldReload = activeSubsetId === undefined && subsets.length > 0;
    setSubsets([]);
    setActiveSubsetId(undefined);
    setChartResetToken((current) => current + 1);
    if (shouldReload) {
      void reloadPreview(selectedAxisKind);
    }
  }

  useEffect(() => {
    if (isScreenTooSmall) {
      return;
    }
    void refreshClasses();
  }, [classSort, isScreenTooSmall]);

  useEffect(() => {
    if (isScreenTooSmall) {
      return;
    }
    void refreshJobs();
    void refreshExcluded();
    return () => {
      cancelClassRequest();
      cancelPreviewRequests();
      clearPreviewLoadingIndicator();
      if (pollTimeoutRef.current !== null) {
        window.clearTimeout(pollTimeoutRef.current);
      }
      subscriptionsRef.current.forEach((unsubscribe) => unsubscribe());
      subscriptionsRef.current.clear();
    };
  }, [isScreenTooSmall]);

  useEffect(() => {
    if (isScreenTooSmall || classesMeta.status !== "building") {
      if (pollTimeoutRef.current !== null) {
        window.clearTimeout(pollTimeoutRef.current);
        pollTimeoutRef.current = null;
      }
      return;
    }
    pollTimeoutRef.current = window.setTimeout(() => {
      void refreshClasses({ silent: true });
      void refreshJobs();
    }, 1500);
    return () => {
      if (pollTimeoutRef.current !== null) {
        window.clearTimeout(pollTimeoutRef.current);
        pollTimeoutRef.current = null;
      }
    };
  }, [classesMeta.status, classSort, isScreenTooSmall]);

  useEffect(() => {
    if (isScreenTooSmall) {
      return;
    }
    applyPreviewTargetReset(true);
    if (!selectedClass) {
      setPreviewState("idle");
      return;
    }
    void loadPreviewSummary(selectedClass, excludedFilter, activeSubsetId);
  }, [activeSubsetId, excludedFilter, isScreenTooSmall, selectedClassKey]);

  useEffect(() => {
    if (isScreenTooSmall || !selectedClass || !selectedAxisKind) {
      return;
    }
    const nextAxisSummary = axisSummary.find((item) => item.axis_kind === selectedAxisKind);
    if (!nextAxisSummary) {
      return;
    }
    if (nextAxisSummary.count > 2000) {
      setSpectra([]);
      setPreviewLimitMessage(
        `当前${getAxisDisplayLabel(nextAxisSummary.axis_kind, nextAxisSummary.axis_unit)}共有 ${nextAxisSummary.count} 条，超过 2000 条渲染上限，请先生成子集。`
      );
      finishPreviewLoading("over-limit");
      return;
    }
    setPreviewLimitMessage(null);
    void loadPreviewDetail(selectedClass, excludedFilter, activeSubsetId, selectedAxisKind);
  }, [activeSubsetId, axisSummary, excludedFilter, isScreenTooSmall, selectedAxisKind, selectedClass]);

  useEffect(() => {
    if (!selectedAxisKind) {
      return;
    }
    if (!axisSummary.some((item) => item.axis_kind === selectedAxisKind)) {
      setSelectedAxisKind(pickPreferredAxisKind(axisSummary));
    }
  }, [axisSummary, selectedAxisKind]);

  useEffect(() => {
    setLockedSpectrum((current) => {
      if (!current) {
        return null;
      }
      return spectra.find((item) => item.id === current.id) ?? null;
    });
  }, [spectra]);

  const rightPanel = (
    <div className="workspace-panel-stack">
      <PathChooserCard
        kind="export"
        currentPath={exportPath}
        onSelect={setExportPath}
        title="导出"
        actionLabel="导出未剔除"
        actionIcon={<CloudDownloadOutlined />}
        onAction={() => startExport("active")}
      />

      <Card title="导出选项" size="small">
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <div className="switch-row">
            <Text>仅导出当前选中分类</Text>
            <Switch checked={exportSelectedOnly} onChange={setExportSelectedOnly} />
          </div>
          <Space wrap>
            <Button onClick={() => void startExport("excluded")}>导出剔除</Button>
            <Button onClick={() => void startExport("all")}>导出全部</Button>
          </Space>
        </Space>
      </Card>

      <Card title="后台任务" size="small" className="scroll-card">
        <div className="scroll-list-wrap jobs-list-wrap">
          {loadingJobs && jobs.length === 0 ? (
            <Skeleton active paragraph={{ rows: 3 }} title={false} />
          ) : (
            <List
              dataSource={jobs}
              locale={{ emptyText: "暂无任务" }}
              renderItem={(job) => {
                const progress = job.total_discovered > 0 ? Math.round((job.processed_count / job.total_discovered) * 100) : job.status === "completed" ? 100 : 0;
                return (
                  <List.Item>
                    <div className="job-item">
                      <div className="job-item-head">
                        <Text strong>
                          {job.type === "import" ? "导入" : job.type === "maintenance" ? "维护" : "导出"} #{job.id}
                        </Text>
                        <Tag color={job.status === "completed" ? "success" : job.status === "failed" ? "error" : "processing"}>
                          {job.status}
                        </Tag>
                      </div>
                      <Progress percent={progress} size="small" />
                      <Text type="secondary">
                        {job.type === "export"
                          ? `范围：${EXPORT_SCOPE_LABELS[String(job.params.scope) as ExportScope] ?? "未知"}${Array.isArray(job.params.class_keys) && job.params.class_keys.length > 0 ? ` · ${job.params.class_keys.length} 个分类` : " · 全部分类"}`
                          : job.progress_message}
                      </Text>
                    </div>
                  </List.Item>
                );
              }}
            />
          )}
        </div>
      </Card>

      <Card title="最近剔除" size="small" className="scroll-card">
        <div className="scroll-list-wrap excluded-list-wrap">
          {loadingRecentExcluded && recentExcluded.length === 0 ? (
            <Skeleton active paragraph={{ rows: 3 }} title={false} />
          ) : (
            <List
              dataSource={recentExcluded}
              locale={{ emptyText: "暂无已剔除光谱" }}
              renderItem={(item) => (
                <List.Item
                  className="recent-item"
                  actions={[
                    <Button key={`restore-${item.id}`} type="link" icon={<UndoOutlined />} onClick={() => void restoreSpectrumItem(item)}>
                      恢复
                    </Button>
                  ]}
                >
                  <List.Item.Meta
                    title={
                      <Tooltip title={item.file_name}>
                        <span className="ellipsis-text">{item.file_name}</span>
                      </Tooltip>
                    }
                    description={item.class_display_name}
                  />
                </List.Item>
              )}
            />
          )}
        </div>
      </Card>
    </div>
  );

  return (
    <ConfigProvider
      theme={{
        token: {
          colorPrimary: "#155eef",
          colorSuccess: "#079455",
          colorWarning: "#dc6803",
          colorError: "#d92d20",
          borderRadius: 18,
          colorBgLayout: "#f5f7fb",
          fontFamily: `"Avenir Next", "PingFang SC", "Noto Sans CJK SC", sans-serif`
        }
      }}
    >
      <AntdApp>
        {messageContextHolder}
        {notificationContextHolder}
        {isScreenTooSmall ? (
          <div className="unsupported-shell">
            <Card className="unsupported-card">
              <Space direction="vertical" size="middle" style={{ width: "100%" }}>
                <Tag icon={<BarChartOutlined />} color="processing">
                  工作台暂不可用
                </Tag>
                <Title level={3} style={{ margin: 0 }}>
                  当前窗口过窄
                </Title>
                <Text type="secondary">
                  这个光谱工作台仅支持较大屏幕。请使用更宽的显示器，或把浏览器窗口扩大到 1280px 以上后再试。
                </Text>
              </Space>
            </Card>
          </div>
        ) : (
          <Layout className="workspace-layout">
            <Sider
              width={336}
              collapsedWidth={0}
              collapsed={!leftPanelVisible}
              trigger={null}
              zeroWidthTriggerStyle={{ display: "none" }}
              className="workspace-sider left-sider"
            >
              <div className="workspace-panel-stack">
                <PathChooserCard
                  kind="import"
                  currentPath={importPath}
                  onSelect={setImportPath}
                  title="导入"
                  actionLabel="开始导入"
                  actionIcon={<CloudUploadOutlined />}
                  onAction={startImport}
                >
                  <div className="switch-row">
                    <div>
                      <Text>校验纤维名称和含量总和</Text>
                      <div>
                        <Text type="secondary">
                          关闭后允许未知名称和非 100% 合计，仍校验 CSV 结构与数值格式
                        </Text>
                      </div>
                    </div>
                    <Switch
                      aria-label="校验纤维名称和含量总和"
                      checked={validateImportLabels}
                      onChange={setValidateImportLabels}
                    />
                  </div>
                </PathChooserCard>

                <Card title="分类总览" extra={<Badge count={filteredClasses.length} color="#155eef" />} className="scroll-card">
                  <Space direction="vertical" size="middle" style={{ width: "100%" }}>
                    <Input.Search
                      allowClear
                      placeholder="搜索分类名称"
                      value={classSearch}
                      onChange={(event) => setClassSearch(event.target.value)}
                    />
                    <Select
                      value={classSort}
                      onChange={setClassSort}
                      options={[
                        { value: "count", label: "按数量" },
                        { value: "component_count", label: "按组分数" },
                        { value: "name", label: "按名称" }
                      ]}
                    />
                    <div className="scroll-list-wrap class-list-wrap">
                      {loadingClasses && classes.length === 0 ? (
                        <Skeleton active paragraph={{ rows: 8 }} title={false} />
                      ) : (
                        <List
                          className="class-list"
                          dataSource={filteredClasses}
                          locale={{ emptyText: classesMeta.status === "building" ? "分类索引初始化中..." : "没有匹配的分类" }}
                          renderItem={(item) => (
                            <List.Item className={`class-list-item ${selectedClass?.class_key === item.class_key ? "is-selected" : ""}`}>
                              <button
                                className="class-select-button"
                                onClick={() => {
                                  setSelectedClass(item);
                                  setSubsets([]);
                                  setActiveSubsetId(undefined);
                                  setPendingUndoSpectrum(null);
                                  clearPreviewState(true);
                                  setChartResetToken((current) => current + 1);
                                }}
                              >
                                <div className="class-list-title">{item.class_display_name}</div>
                                <Space wrap size={[4, 8]}>
                                  <Tag>{item.component_count} 组分</Tag>
                                  <Tag color="blue">总数 {item.total_count}</Tag>
                                  <Tag color="success">有效 {item.active_count}</Tag>
                                  <Tag color="default">剔除 {item.excluded_count}</Tag>
                                </Space>
                              </button>
                            </List.Item>
                          )}
                        />
                      )}
                    </div>
                  </Space>
                </Card>
              </div>
            </Sider>

            <Layout>
              <Content className="workspace-content">
                <div className="workspace-control-rail">
                  <Button
                    className={`panel-toggle-button ${leftPanelVisible ? "is-expanded" : "is-collapsed"}`}
                    icon={leftPanelVisible ? <MenuFoldOutlined /> : <MenuUnfoldOutlined />}
                    onClick={() => setLeftPanelVisible((current) => !current)}
                  >
                    <span className="panel-toggle-label">{leftPanelVisible ? "收起分类" : "显示分类"}</span>
                  </Button>
                  <Button
                    className={`panel-toggle-button ${rightPanelVisible ? "is-expanded" : "is-collapsed"}`}
                    icon={rightPanelVisible ? <MenuFoldOutlined /> : <MenuUnfoldOutlined />}
                    onClick={() => setRightPanelVisible((current) => !current)}
                  >
                    <span className="panel-toggle-label">{rightPanelVisible ? "收起任务" : "显示任务"}</span>
                  </Button>
                </div>

                {classesMeta.status === "building" && (
                  <Alert
                    type="info"
                    showIcon
                    message="分类索引初始化中"
                    description={classesMeta.progress_message ?? "检测到已有光谱数据，正在构建分类缓存，页面会自动刷新。"}
                  />
                )}

                <Card className="status-strip" size="small">
                  {loadingClasses && classes.length === 0 ? (
                    <Skeleton active title={false} paragraph={{ rows: 1 }} />
                  ) : (
                    <div className="status-grid">
                      <Statistic title="分类数量" value={classes.length} valueStyle={{ fontSize: 24, lineHeight: 1.1 }} />
                      <Statistic title="当前分类总量" value={selectedClass?.total_count ?? 0} valueStyle={{ fontSize: 24, lineHeight: 1.1 }} />
                      <Statistic title="当前载入曲线" value={previewSpectra.length} valueStyle={{ fontSize: 24, lineHeight: 1.1 }} />
                      <Statistic title="最近任务数" value={jobs.length} valueStyle={{ fontSize: 24, lineHeight: 1.1 }} />
                    </div>
                  )}
                </Card>

                <Card
                  size="small"
                  title="预览控制"
                  extra={
                    <Space wrap>
                      <Select
                        value={excludedFilter}
                        style={{ width: 128 }}
                        onChange={setExcludedFilter}
                        options={[
                          { value: "active", label: "仅有效" },
                          { value: "excluded", label: "仅剔除" },
                          { value: "all", label: "全部" }
                        ]}
                      />
                      <Button
                        aria-label="撤销最近剔除"
                        icon={<UndoOutlined />}
                        disabled={!pendingUndoSpectrum}
                        onClick={() => void handleUndo()}
                      >
                        撤销最近剔除
                      </Button>
                      <Button
                        icon={<ReloadOutlined />}
                        disabled={!selectedClass}
                        onClick={() => void reloadPreview(selectedAxisKind, { bypassCache: true })}
                      >
                        刷新
                      </Button>
                    </Space>
                  }
                >
                  <Space direction="vertical" size="middle" style={{ width: "100%" }}>
                    <div className="subset-toolbar-inline">
                      <div className="subset-mode-control">
                        <Segmented
                          value={subsetMode}
                          onChange={(value) => setSubsetMode(value as "count" | "ratio")}
                          options={[
                            { label: "按数量切分", value: "count" },
                            { label: "按比例切分", value: "ratio" }
                          ]}
                        />
                        <div className="subset-input-wrap">
                          {subsetMode === "count" ? (
                            <InputNumber
                              min={1}
                              style={{ width: "100%" }}
                              value={Number(subsetInput) || 1}
                              onChange={(value) => setSubsetInput(String(value ?? 1))}
                            />
                          ) : (
                            <Input value={subsetInput} onChange={(event) => setSubsetInput(event.target.value)} placeholder="例如 2 表示均分为 2 份" />
                          )}
                        </div>
                      </div>
                      <Space wrap>
                        <Button loading={creatingSubsets} onClick={() => void createSubsets()} disabled={!selectedClass || creatingSubsets}>
                          生成子集
                        </Button>
                        <Button onClick={closeSubsets} disabled={subsets.length === 0 && !activeSubsetId}>
                          返回全部
                        </Button>
                      </Space>
                    </div>
                    <Text type="secondary">
                      {subsetMode === "count"
                        ? "按数量切分：输入每个子集的条数，例如 20 表示每个子集 20 条。"
                        : "按比例切分：输入等分份数，例如 2 表示切成 50% / 50%。"}
                    </Text>

                    {subsets.length > 0 && (
                      <Tabs
                        activeKey={activeSubsetId ?? "all"}
                        onChange={(key) => setActiveSubsetId(key === "all" ? undefined : key)}
                        items={[
                          { key: "all", label: "全部" },
                          ...subsets.map((subset) => ({
                            key: subset.subset_id,
                            label: `子集${subset.index} (${subset.count})`
                          }))
                        ]}
                      />
                    )}
                  </Space>
                </Card>

                <Card
                  size="small"
                  title="光谱预览"
                  extra={
                    <Space split={<Divider type="vertical" />} size="middle" wrap>
                      <Tooltip title={selectedClassLabel}>
                        <Tag icon={<BarChartOutlined />} className="current-class-pill">
                          {selectedClassLabel}
                        </Tag>
                      </Tooltip>
                      {selectedAxisLabel && <Tag>{selectedAxisLabel}</Tag>}
                      <Text type="secondary">可预览：{previewSpectra.length}</Text>
                      <Button size="small" icon={<ReloadOutlined />} onClick={() => setChartResetToken((current) => current + 1)} disabled={!selectedClass || !canRender || previewSpectra.length === 0}>
                        恢复默认缩放
                      </Button>
                    </Space>
                  }
                >
                  {!selectedClass && <Empty description="请选择左侧分类后开始预览" />}
                  {selectedClass && axisSummary.length > 1 && (
                    <div className="axis-switch-wrap">
                      <Segmented
                        value={selectedAxisKind}
                        onChange={(value) => {
                          setLockedSpectrum(null);
                          setSelectedAxisKind(value as AxisKind);
                        }}
                        options={axisSummary.map((item) => ({
                          value: item.axis_kind,
                          label: `${getAxisDisplayLabel(item.axis_kind, item.axis_unit)} (${item.count})`
                        }))}
                      />
                    </div>
                  )}
                  {selectedClass && isPreviewLoading && (
                    <div className="preview-loading-shell">
                      <Skeleton active title={false} paragraph={{ rows: 7 }} />
                      {loadingIndicatorVisible && (
                        <div className="preview-progress-wrap">
                          <Progress percent={previewLoad.percent} status="active" />
                          <Text type="secondary">{previewLoad.message}</Text>
                        </div>
                      )}
                    </div>
                  )}
                  {selectedClass && previewState === "ready" && chartDensityMode === "medium" && canRender && previewSpectra.length > 0 && (
                    <Alert
                      type="info"
                      showIcon
                      message="中等密度模式"
                      description="当前曲线较多，悬停命中已节流优化，点击锁定与删除仍可正常使用。"
                    />
                  )}
                  {selectedClass && previewState === "ready" && chartDensityMode === "dense" && canRender && previewSpectra.length > 0 && (
                    <Alert
                      type="warning"
                      showIcon
                      message="高密度模式"
                      description="当前曲线超过 800 条，已关闭悬停预览和悬停高亮，以保证缩放、平移和点击操作流畅。"
                    />
                  )}
                  {selectedClass && previewState === "ready" && previewLimitMessage && (
                    <Alert
                      type="warning"
                      showIcon
                      message="预览已按点数预算截断"
                      description={previewLimitMessage}
                    />
                  )}
                  {selectedClass && previewState === "over-limit" && previewLimitMessage && (
                    <Alert
                      type="warning"
                      showIcon
                      message="当前轴类型超过渲染上限"
                      description={previewLimitMessage}
                    />
                  )}
                  {selectedClass && previewState === "empty" && (
                    <Empty
                      description={
                        excludedFilter === "excluded"
                          ? `当前${selectedAxisLabel ?? "所选轴类型"}没有可预览的已剔除光谱。`
                          : excludedFilter === "all"
                            ? `当前${selectedAxisLabel ?? "所选轴类型"}没有可预览的光谱。`
                            : `当前${selectedAxisLabel ?? "所选轴类型"}没有可预览的未剔除光谱。`
                      }
                    />
                  )}
                  {selectedClass && previewState === "ready" && canRender && (
                    <Suspense fallback={<Spin size="large" />}>
                      <SpectrumChart
                        key={`${selectedClass.class_key}:${excludedFilter}:${selectedAxisKind ?? "all"}:${activeSubsetId ?? "all"}`}
                        spectra={previewSpectra}
                        resetSignal={chartResetToken}
                        lockedSpectrumId={lockedSpectrum?.id ?? null}
                        interactionMode={chartDensityMode}
                        onLockSpectrum={setLockedSpectrum}
                        onQuickExclude={(spectrum) => void handleExclude(spectrum)}
                      />
                    </Suspense>
                  )}
                </Card>

                <DetailCard
                  compact
                  spectrum={detailSpectrum}
                  modeLabel={detailModeLabel}
                  onExclude={handleExclude}
                  onRestore={restoreSpectrumItem}
                  onClearLock={clearLockedSpectrum}
                />

                {errorText && <Alert type="error" showIcon message="前端操作失败" description={errorText} />}
              </Content>
            </Layout>

            <Sider
              width={352}
              collapsedWidth={0}
              collapsed={!rightPanelVisible}
              trigger={null}
              zeroWidthTriggerStyle={{ display: "none" }}
              className="workspace-sider right-sider"
            >
              {rightPanel}
            </Sider>
          </Layout>
        )}
      </AntdApp>
    </ConfigProvider>
  );
}

export default function App() {
  return <Workspace />;
}
