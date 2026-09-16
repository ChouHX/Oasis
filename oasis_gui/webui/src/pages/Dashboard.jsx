import { useEffect, useRef, useState } from "react";
import {
  Card, Row, Col, Statistic, Button, Select, InputNumber, Space, Typography,
  Tag, App as AntApp, Checkbox,
} from "antd";
import { CaretRightOutlined, PauseOutlined, ClearOutlined } from "@ant-design/icons";
import { api, STATUS } from "../api.js";

const { Text } = Typography;

const LEVEL = { ok: "#52c41a", error: "#ff4d4f", warn: "#fa8c16", info: "#262626" };

function LogPanel() {
  const [lines, setLines] = useState([]);
  const [total, setTotal] = useState(0);
  const [auto, setAuto] = useState(true);
  const seq = useRef(0);
  const box = useRef(null);

  useEffect(() => {
    let stop = false;
    const tick = async () => {
      try {
        const d = await api.log(seq.current);
        if (stop || d.seq === undefined) return;
        seq.current = d.seq;
        setTotal(d.seq);
        if (d.rows.length) {
          setLines((prev) => {
            const next = prev.concat(d.rows);
            // the server keeps 2000 lines; the DOM does not need all of them
            return next.length > 1500 ? next.slice(next.length - 1500) : next;
          });
        }
      } catch (e) { /* auth handled by the app poller */ }
    };
    tick();
    const t = setInterval(tick, 2000);
    return () => { stop = true; clearInterval(t); };
  }, []);

  useEffect(() => {
    if (auto && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [lines, auto]);

  return (
    <Card
      title="实时日志"
      size="small"
      extra={
        <Space size={12}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {lines.length} 行 · 共 {total} 条
          </Text>
          <Checkbox checked={auto} onChange={(e) => setAuto(e.target.checked)}>
            自动滚动
          </Checkbox>
          <Button size="small" icon={<ClearOutlined />}
                  onClick={() => { setLines([]); }}>
            清屏
          </Button>
        </Space>
      }
      styles={{ body: { padding: 0 } }}
    >
      <div
        ref={box}
        style={{
          height: "46vh", minHeight: 220, overflow: "auto", background: "#fafafa",
          borderTop: "1px solid #f0f0f0", padding: "10px 14px",
          fontFamily: "Consolas, Menlo, monospace", fontSize: 12, lineHeight: 1.6,
          whiteSpace: "pre-wrap", wordBreak: "break-word",
        }}
      >
        {lines.length === 0
          ? <Text type="secondary">暂无日志</Text>
          : lines.map((l, i) => (
              <div key={l.n ?? i} style={{ color: LEVEL[l.level] || "#262626" }}>
                [{new Date(l.at * 1000).toLocaleTimeString("zh-CN", { hour12: false })}] {l.msg}
              </div>
            ))}
      </div>
    </Card>
  );
}

export default function Dashboard({ state, refresh }) {
  const { message } = AntApp.useApp();
  const [mode, setMode] = useState("browser");
  const [threads, setThreads] = useState(2);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!state?.config) return;
    if (state.config.mode) setMode(state.config.mode);
    if (state.config.threads) setThreads(state.config.threads);
  }, [state?.config?.mode, state?.config?.threads]);

  const stats = state?.stats || {};
  const host = state?.host || {};

  const run = async (fn, ok) => {
    setBusy(true);
    try {
      await fn();
      if (ok) message.success(ok);
      refresh();
    } catch (e) {
      message.error(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };

  const recommended = host.recommended;

  return (
    <>
      <Row gutter={[12, 12]} style={{ marginBottom: 12 }}>
        {Object.entries(STATUS).map(([key, meta]) => (
          <Col key={key} xs={12} sm={8} md={6} lg={3} flex="1 1 130px">
            <Card size="small">
              <Statistic
                title={meta.label}
                value={stats[key] ?? 0}
                valueStyle={{ color: meta.color, fontSize: 22 }}
              />
            </Card>
          </Col>
        ))}
      </Row>

      <Card title="运行控制" size="small" style={{ marginBottom: 12 }}>
        <Space wrap size={12}>
          <Space size={6}>
            <Text type="secondary">模式</Text>
            <Select
              value={mode}
              onChange={setMode}
              style={{ width: 260 }}
              disabled={state?.running}
              options={[
                { value: "browser", label: "浏览器（Playwright 全流程）" },
                { value: "hybrid", label: "混合（共享浏览器取 captcha）" },
              ]}
            />
          </Space>
          <Space size={6}>
            <Text type="secondary">线程</Text>
            <InputNumber min={1} max={64} value={threads} onChange={setThreads}
                         disabled={state?.running} />
          </Space>
          <Button
            type="primary"
            icon={<CaretRightOutlined />}
            loading={busy}
            disabled={state?.running}
            onClick={() => run(() => api.start(threads, mode), "已启动")}
          >
            开始注册
          </Button>
          <Button
            danger
            icon={<PauseOutlined />}
            disabled={!state?.running}
            onClick={() => run(api.stop, "已请求停止（跑完当前账号后停下）")}
          >
            停止
          </Button>
          {recommended && (
            <Tag color="blue" style={{ cursor: "pointer" }}
                 onClick={() => setThreads(recommended)}>
              建议 {recommended} 线程
            </Tag>
          )}
        </Space>
        {host.total_mb && (
          <div style={{ marginTop: 10 }}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              内存 {(host.total_mb - host.available_mb)} / {host.total_mb} MB 在用 ·
              可用 {host.available_mb} MB · {host.cores} 核 —— {host.reason}
            </Text>
          </div>
        )}
      </Card>

      <LogPanel />
    </>
  );
}
