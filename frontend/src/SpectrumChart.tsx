import { useEffect, useMemo, useRef, useState } from "react";
import { LineChart } from "echarts/charts";
import { DataZoomComponent, GridComponent, TooltipComponent } from "echarts/components";
import * as echarts from "echarts/core";
import type { EChartsType } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { Tag } from "antd";
import {
  buildHitTestSpectra,
  findNearestSpectrumHit,
  formatAxisValue,
  formatSpectrumLabels,
  getXAxisTitle,
  getSpectraExtents,
  shiftZoomWindow
} from "./chartInteraction";
import type { HitTestSpectrum } from "./chartInteraction";
import type { SpectrumItem } from "./types";

echarts.use([LineChart, GridComponent, DataZoomComponent, TooltipComponent, CanvasRenderer]);

type HoverPreview = {
  spectrum: SpectrumItem;
  left: number;
  top: number;
};

type Props = {
  spectra: SpectrumItem[];
  resetSignal: number;
  lockedSpectrumId: number | null;
  interactionMode: "full" | "medium" | "dense";
  onLockSpectrum: (spectrum: SpectrumItem | null) => void;
  onQuickExclude: (spectrum: SpectrumItem) => void;
};

type ZoomWindow = {
  xStart: number;
  xEnd: number;
  yStart: number;
  yEnd: number;
};

const X_INSIDE_ZOOM_ID = "x-inside-zoom";
const Y_INSIDE_ZOOM_ID = "y-inside-zoom";
const X_SLIDER_ZOOM_ID = "x-slider-zoom";
const DEFAULT_ZOOM_WINDOW: ZoomWindow = {
  xStart: 0,
  xEnd: 100,
  yStart: 0,
  yEnd: 100
};

function readZoomWindowFromOption(option: {
  dataZoom?: Array<{ id?: string; start?: number; end?: number }>;
}): ZoomWindow {
  const currentZooms = option.dataZoom ?? [];
  const readById = (id: string) => currentZooms.find((item) => item.id === id);
  return {
    xStart: readById(X_INSIDE_ZOOM_ID)?.start ?? readById(X_SLIDER_ZOOM_ID)?.start ?? DEFAULT_ZOOM_WINDOW.xStart,
    xEnd: readById(X_INSIDE_ZOOM_ID)?.end ?? readById(X_SLIDER_ZOOM_ID)?.end ?? DEFAULT_ZOOM_WINDOW.xEnd,
    yStart: readById(Y_INSIDE_ZOOM_ID)?.start ?? DEFAULT_ZOOM_WINDOW.yStart,
    yEnd: readById(Y_INSIDE_ZOOM_ID)?.end ?? DEFAULT_ZOOM_WINDOW.yEnd
  };
}

export function SpectrumChart({
  spectra,
  resetSignal,
  lockedSpectrumId,
  interactionMode,
  onLockSpectrum,
  onQuickExclude
}: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<EChartsType | null>(null);
  const animationFrameRef = useRef<number | null>(null);
  const latestPointerRef = useRef<{ x: number; y: number } | null>(null);
  const hitTestSpectraRef = useRef<HitTestSpectrum[]>([]);
  const lockCallbackRef = useRef(onLockSpectrum);
  const quickExcludeCallbackRef = useRef(onQuickExclude);
  const optionRef = useRef<Record<string, unknown> | null>(null);
  const zoomWindowRef = useRef<ZoomWindow>(DEFAULT_ZOOM_WINDOW);
  const lastHoverAtRef = useRef(0);
  const panStateRef = useRef<{
    startPixel: { x: number; y: number };
    startZoom: ZoomWindow;
    dataPerPixelX: number;
    dataPerPixelY: number;
    moved: boolean;
  } | null>(null);
  const ignoreNextClickRef = useRef(false);
  const [preview, setPreview] = useState<HoverPreview | null>(null);
  const [hoveredSpectrumId, setHoveredSpectrumId] = useState<number | null>(null);

  useEffect(() => {
    lockCallbackRef.current = onLockSpectrum;
  }, [onLockSpectrum]);

  useEffect(() => {
    quickExcludeCallbackRef.current = onQuickExclude;
  }, [onQuickExclude]);

  const hitTestSpectra = useMemo(() => buildHitTestSpectra(spectra, 512), [spectra]);
  const axisExtents = useMemo(() => getSpectraExtents(spectra), [spectra]);
  const activeSpectrumId = interactionMode === "full" ? hoveredSpectrumId ?? lockedSpectrumId : lockedSpectrumId;

  useEffect(() => {
    hitTestSpectraRef.current = hitTestSpectra;
  }, [hitTestSpectra]);

  useEffect(() => {
    if (interactionMode === "dense") {
      setPreview(null);
      setHoveredSpectrumId(null);
    }
  }, [interactionMode]);

  const option = useMemo(() => {
    const yLabelRich = {
      axisValue: {
        width: 74,
        align: "right",
        fontFamily: '"SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace'
      }
    };
    const xLabelRich = {
      axisValue: {
        width: 76,
        align: "center",
        fontFamily: '"SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace'
      }
    };
    return {
      animation: false,
      backgroundColor: "#fcfcfd",
      tooltip: { show: false },
      grid: { left: 118, right: 36, top: 36, bottom: 102, containLabel: false },
      xAxis: {
        type: "value",
        name: getXAxisTitle(spectra),
        nameLocation: "middle",
        nameGap: 46,
        nameTextStyle: { fontWeight: 600, padding: [18, 0, 0, 0] },
        min: axisExtents.xExtent[0],
        max: axisExtents.xExtent[1],
        scale: true,
        splitNumber: 6,
        axisLabel: {
          color: "#475467",
          margin: 12,
          hideOverlap: true,
          formatter: (value: number) => `{axisValue|${formatAxisValue(value, "x")}}`,
          rich: xLabelRich
        },
        splitLine: { lineStyle: { color: "rgba(15, 23, 42, 0.08)" } }
      },
      yAxis: {
        type: "value",
        name: "吸光度",
        nameLocation: "middle",
        nameGap: 74,
        nameTextStyle: { fontWeight: 600, padding: [0, 0, 14, 0] },
        scale: true,
        splitNumber: 6,
        axisLabel: {
          color: "#475467",
          margin: 14,
          formatter: (value: number) => `{axisValue|${formatAxisValue(value, "y")}}`,
          rich: yLabelRich
        },
        splitLine: { lineStyle: { color: "rgba(15, 23, 42, 0.08)" } }
      },
      dataZoom: [
        {
          id: X_INSIDE_ZOOM_ID,
          type: "inside",
          xAxisIndex: 0,
          filterMode: "none",
          moveOnMouseMove: false,
          moveOnMouseWheel: false,
          zoomOnMouseWheel: true
        },
        {
          id: Y_INSIDE_ZOOM_ID,
          type: "inside",
          yAxisIndex: 0,
          filterMode: "none",
          moveOnMouseMove: false,
          moveOnMouseWheel: false,
          zoomOnMouseWheel: true
        },
        {
          id: X_SLIDER_ZOOM_ID,
          type: "slider",
          xAxisIndex: 0,
          filterMode: "none",
          bottom: 8,
          height: 18,
          borderColor: "rgba(15, 23, 42, 0.08)"
        }
      ],
      series: spectra.map((spectrum, index) => {
        const isActive = activeSpectrumId === spectrum.id;
        return {
          type: "line",
          name: spectrum.file_name,
          showSymbol: false,
          sampling: "lttb",
          progressive: 300,
          progressiveThreshold: 600,
          silent: true,
          lineStyle: {
            width: isActive ? 2.6 : 1.1,
            opacity: isActive ? 0.98 : spectrum.is_excluded ? 0.14 : 0.24
          },
          z: isActive ? 10 : 2,
          data: spectrum.x_values.map((xValue, pointIndex) => [xValue, spectrum.y_values[pointIndex]]),
          color: palette[index % palette.length]
        };
      })
    };
  }, [activeSpectrumId, spectra]);

  useEffect(() => {
    if (!containerRef.current) {
      return;
    }

    const chart = echarts.init(containerRef.current, undefined, { renderer: "canvas" });
    chartRef.current = chart;
    const resizeObserver = new ResizeObserver(() => chart.resize());
    resizeObserver.observe(containerRef.current);
    const preventContextMenu = (event: MouseEvent) => event.preventDefault();
    containerRef.current.addEventListener("contextmenu", preventContextMenu);

    const clearHover = () => {
      setPreview(null);
      setHoveredSpectrumId(null);
      if (animationFrameRef.current !== null) {
        cancelAnimationFrame(animationFrameRef.current);
        animationFrameRef.current = null;
      }
    };

    const readCurrentZoomWindow = (): ZoomWindow =>
      readZoomWindowFromOption(
        chart.getOption() as {
          dataZoom?: Array<{ id?: string; start?: number; end?: number }>;
        }
      );

    const dispatchZoomWindow = (nextZoom: ZoomWindow) => {
      zoomWindowRef.current = nextZoom;
      chart.dispatchAction({
        type: "dataZoom",
        batch: [
          { dataZoomId: X_INSIDE_ZOOM_ID, start: nextZoom.xStart, end: nextZoom.xEnd },
          { dataZoomId: X_SLIDER_ZOOM_ID, start: nextZoom.xStart, end: nextZoom.xEnd },
          { dataZoomId: Y_INSIDE_ZOOM_ID, start: nextZoom.yStart, end: nextZoom.yEnd }
        ]
      });
    };

    const resolveHitAtPointer = (pointer: { x: number; y: number } | null) => {
      const chartInstance = chartRef.current;
      if (!chartInstance || !pointer) {
        return null;
      }
      if (!chartInstance.containPixel({ gridIndex: 0 }, [pointer.x, pointer.y])) {
        return null;
      }

      const dataPoint = chartInstance.convertFromPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [pointer.x, pointer.y]) as number[] | undefined;
      const thresholdPoint = chartInstance.convertFromPixel(
        { xAxisIndex: 0, yAxisIndex: 0 },
        [pointer.x, pointer.y + 16]
      ) as number[] | undefined;
      if (!Array.isArray(dataPoint) || dataPoint.length < 2) {
        return null;
      }

      const yThreshold = Math.max(Math.abs((thresholdPoint?.[1] ?? dataPoint[1]) - dataPoint[1]), 0.0025);
      return findNearestSpectrumHit({
        spectra: hitTestSpectraRef.current,
        xValue: dataPoint[0],
        yValue: dataPoint[1],
        yThreshold
      });
    };

    const quickExcludeAtPointer = (pointer: { x: number; y: number } | null) => {
      const hit = resolveHitAtPointer(pointer);
      if (!hit) {
        return;
      }
      clearHover();
      quickExcludeCallbackRef.current(hit.spectrum);
    };

    const syncZoomWindow = () => {
      zoomWindowRef.current = readCurrentZoomWindow();
    };

    const updateHover = () => {
      animationFrameRef.current = null;
      const pointer = latestPointerRef.current;
      if (interactionMode === "dense") {
        clearHover();
        return;
      }
      const hit = resolveHitAtPointer(pointer);
      if (!pointer || !hit) {
        clearHover();
        return;
      }

      setHoveredSpectrumId(hit.spectrum.id);
      setPreview({
        spectrum: hit.spectrum,
        left: pointer.x + 16,
        top: pointer.y + 16
      });
    };

    const scheduleHover = (x: number, y: number) => {
      if (interactionMode === "dense") {
        return;
      }
      const now = performance.now();
      if (interactionMode === "medium" && now - lastHoverAtRef.current < 50) {
        return;
      }
      lastHoverAtRef.current = now;
      latestPointerRef.current = { x, y };
      if (animationFrameRef.current !== null) {
        return;
      }
      animationFrameRef.current = requestAnimationFrame(updateHover);
    };

    chart.on("datazoom", syncZoomWindow);

    chart.getZr().on("mousemove", (event) => {
      const panState = panStateRef.current;
      if (panState) {
        const pixelDeltaX = event.offsetX - panState.startPixel.x;
        const pixelDeltaY = event.offsetY - panState.startPixel.y;
        const deltaX = -pixelDeltaX * panState.dataPerPixelX;
        const deltaY = -pixelDeltaY * panState.dataPerPixelY;
        const xSpan = axisExtents.xExtent[1] - axisExtents.xExtent[0];
        const ySpan = axisExtents.yExtent[1] - axisExtents.yExtent[0];
        const xDeltaPercent = xSpan > 0 ? (deltaX / xSpan) * 100 : 0;
        const yDeltaPercent = ySpan > 0 ? (deltaY / ySpan) * 100 : 0;
        const [nextXStart, nextXEnd] = shiftZoomWindow(panState.startZoom.xStart, panState.startZoom.xEnd, xDeltaPercent);
        const [nextYStart, nextYEnd] = shiftZoomWindow(panState.startZoom.yStart, panState.startZoom.yEnd, yDeltaPercent);
        panState.moved =
          panState.moved ||
          Math.abs(event.offsetX - panState.startPixel.x) > 2 ||
          Math.abs(event.offsetY - panState.startPixel.y) > 2;
        dispatchZoomWindow({
          xStart: nextXStart,
          xEnd: nextXEnd,
          yStart: nextYStart,
          yEnd: nextYEnd
        });
        return;
      }
      scheduleHover(event.offsetX, event.offsetY);
    });
    chart.getZr().on("mousedown", (event) => {
      const nativeEvent = event.event as MouseEvent | undefined;
      const button = nativeEvent?.button ?? 0;
      if (!chart.containPixel({ gridIndex: 0 }, [event.offsetX, event.offsetY])) {
        return;
      }
      if (button === 2) {
        nativeEvent?.preventDefault?.();
        quickExcludeAtPointer({ x: event.offsetX, y: event.offsetY });
        return;
      }
      if (button !== 1) {
        return;
      }
      nativeEvent?.preventDefault?.();
      clearHover();
      const ref0 = chart.convertFromPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [0, 0]);
      const ref1 = chart.convertFromPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [1, 1]);
      panStateRef.current = {
        startPixel: { x: event.offsetX, y: event.offsetY },
        startZoom: zoomWindowRef.current,
        dataPerPixelX: ref1[0] - ref0[0],
        dataPerPixelY: ref1[1] - ref0[1],
        moved: false
      };
    });
    chart.getZr().on("mouseup", () => {
      if (panStateRef.current?.moved) {
        ignoreNextClickRef.current = true;
      }
      panStateRef.current = null;
    });
    chart.getZr().on("globalout", () => {
      panStateRef.current = null;
      clearHover();
    });
    chart.getZr().on("click", (event) => {
      const nativeEvent = event.event as MouseEvent | undefined;
      if ((nativeEvent?.button ?? 0) !== 0) {
        return;
      }
      if (ignoreNextClickRef.current) {
        ignoreNextClickRef.current = false;
        return;
      }
      const hit = resolveHitAtPointer({ x: event.offsetX, y: event.offsetY });
      if (!hit) {
        lockCallbackRef.current(null);
        return;
      }
      if (nativeEvent?.shiftKey) {
        quickExcludeAtPointer({ x: event.offsetX, y: event.offsetY });
        return;
      }
      lockCallbackRef.current(hit.spectrum);
    });

    return () => {
      clearHover();
      panStateRef.current = null;
      chart.off("datazoom", syncZoomWindow);
      containerRef.current?.removeEventListener("contextmenu", preventContextMenu);
      resizeObserver.disconnect();
      chart.dispose();
      chartRef.current = null;
    };
  }, [axisExtents.xExtent, axisExtents.yExtent, interactionMode]);

  useEffect(() => {
    if (chartRef.current) {
      optionRef.current = option as Record<string, unknown>;
      chartRef.current.setOption(option, { notMerge: false, lazyUpdate: true });
      zoomWindowRef.current = readZoomWindowFromOption(
        chartRef.current.getOption() as {
          dataZoom?: Array<{ id?: string; start?: number; end?: number }>;
        }
      );
    }
  }, [option]);

  useEffect(() => {
    if (chartRef.current && optionRef.current) {
      chartRef.current.setOption(optionRef.current, true);
      zoomWindowRef.current = DEFAULT_ZOOM_WINDOW;
      setPreview(null);
      setHoveredSpectrumId(null);
    }
  }, [resetSignal]);

  return (
    <div className="chart-shell">
      <div className="chart-surface" ref={containerRef} />
      {preview && interactionMode !== "dense" && (
        <div
          className="chart-hover-card"
          style={{
            left: `${Math.min(preview.left, Math.max(24, (containerRef.current?.clientWidth ?? 0) - 260))}px`,
            top: `${preview.top}px`
          }}
          role="tooltip"
        >
          <div className="chart-hover-title">{preview.spectrum.file_name}</div>
          <div className="chart-hover-subtitle">{formatSpectrumLabels(preview.spectrum.labels) || "无标签"}</div>
          <div className="chart-hover-tags">
            <Tag color={preview.spectrum.is_excluded ? "default" : "processing"}>
              {preview.spectrum.is_excluded ? "已剔除" : "有效"}
            </Tag>
            <Tag>{preview.spectrum.class_display_name}</Tag>
          </div>
        </div>
      )}
    </div>
  );
}

const palette = [
  "#155eef",
  "#087443",
  "#dd6b20",
  "#7a5af8",
  "#0086c9",
  "#be123c",
  "#364152",
  "#0e9384",
  "#ef4444",
  "#b54708"
];
