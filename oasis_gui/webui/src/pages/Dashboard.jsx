import { useEffect, useRef, useState } from "react";
import {
  Card, Row, Col, Statistic, Button, InputNumber, Space, Typography,
  Tag, Tooltip, App as AntApp, Checkbox, Switch,
} from "antd";
import {
  CaretRightOutlined, PauseOutlined, ClearOutlined, QuestionCircleOutlined,
  SyncOutlined,
} from "@ant-design/icons";
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
  const [threads, setThreads] = useState(2);
  const [interval, setInterval] = useState(300);

  const [skipHits, setSkipHits] = useState(true);
  const [mailFilter, setMailFilter] = useState(true);
  const [onlyOpted, setOnlyOpted] = useState(true);
  const [busy, setBusy] = useState(false);

  // Seed the run controls once per visit, and never again.
  //
  // /api/state is polled every three seconds and each reply is a brand new
  // object, so an effect keyed on it re-ran on every poll and put the server's
  // stored values back over whatever had just been typed. That is how a control
  // quietly reverted to its old value before "开始检测" read it. Seeding once
  // leaves the controls alone while someone is editing them; leaving the page
  // and coming back reseeds from the server.
  const seeded = useRef(false);
  useEffect(() => {
    const c = state?.config;
    if (!c || seeded.current) return;
    seeded.current = true;
    if (c.threads) setThreads(c.threads);
    if (c.interval) setInterval(c.interval);

    if (c.skip_hits !== undefined) setSkipHits(!!c.skip_hits);
    if (c.mail_filter !== undefined) setMailFilter(!!c.mail_filter);
    if (c.only_opted !== undefined) setOnlyOpted(!!c.only_opted);
  }, [state?.config]);

  const stats = state?.stats || {};
  const host = state?.host || {};
  const monitor = state?.monitor || {};

  // Persist the controls as they are changed, not only when a sweep starts.
  // They are settings, and a number field fires on every keystroke while each
  // write goes to disk - hence the debounce.
  const saveTimer = useRef(null);
  const persist = (patch) => {
    clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => {
      api.saveConfig(patch).catch(() => {});
    }, 700);
  };

  const changeThreads = (v) => { setThreads(v); if (v) persist({ threads: v }); };
  const changeInterval = (v) => { setInterval(v); if (v) persist({ interval: v }); };
  const changeSkipHits = (v) => { setSkipHits(v); persist({ skip_hits: v }); };
  const changeMailFilter = (v) => { setMailFilter(v); persist({ mail_filter: v }); };
  const changeOnlyOpted = (v) => { setOnlyOpted(v); persist({ only_opted: v }); };

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

      <Card
        title="检测控制"
        size="small"
        style={{ marginBottom: 12 }}
        extra={
          <Text type="secondary" style={{ fontSize: 12 }}>
            {state?.running
              ? `巡检中 · 第 ${monitor.round || 0} 轮 · 本轮已读 ${monitor.checked || 0}`
              : "空闲"}
          </Text>
        }
      >
        <Space wrap size={12}>
          <Space size={6}>
            <Text type="secondary">并发</Text>
            <InputNumber min={1} max={32} value={threads}
                         onChange={changeThreads} />
            <Tooltip title="同时打开几条收件箱连接。同一个 Gmail 收件箱下的 iCloud 别名共用一条连接，所以这是并发连接数，不是「同时读几封信」。">
              <QuestionCircleOutlined style={{ color: "#8c8c8c" }} />
            </Tooltip>
          </Space>
          <Space size={6}>
            <Text type="secondary">间隔（秒）</Text>
            <InputNumber min={30} max={86400} step={30} value={interval}
                         onChange={changeInterval} style={{ width: 108 }} />
            <Tooltip title="一轮跑完到下一轮开始之间的等待。中签通知不是秒级事件，频率再高只是把对方的收件箱打成请求尖峰。">
              <QuestionCircleOutlined style={{ color: "#8c8c8c" }} />
            </Tooltip>
          </Space>
          <Space size={6}>
            <Text type="secondary">注册截止</Text>
            <Text code style={{ fontSize: 12 }}>{state?.cutoff || "—"}</Text>
            <Tooltip title="Oasis 官方：Registration closes on Thursday 17 September at 4pm BST / 5pm CEST / 8am PT / 11am ET。中签结果信只可能出现在这之后 —— 此前的每一封 Oasis 来信都只说明「预约成功了」，一张票都没拿到。在设置页可以改这个时间。">
              <QuestionCircleOutlined style={{ color: "#8c8c8c" }} />
            </Tooltip>
          </Space>
          <Space size={6}>
            <Text type="secondary">跳过已中签</Text>
            <Switch size="small" checked={skipHits} onChange={changeSkipHits} />
          </Space>
          <Space size={6}>
            <Text type="secondary">只查已预约</Text>
            <Tooltip title="没预约过的邮箱收不到中签信，扫它没有意义。默认只检测标记为「已预约」的地址 —— 判定来自上一版程序的成功记录、导入时的勾选、以及邮箱池页的手工标记。关掉它则整池都查。">
              <Switch size="small" checked={onlyOpted}
                      onChange={changeOnlyOpted} />
            </Tooltip>
          </Space>
          <Space size={6}>
            <Text type="secondary">只拉 Oasis 来信</Text>
            <Tooltip title="让服务端只把 Oasis 的信拉回来（发件人 openstage 或标题含 Oasis），而不是把「最近 N 封」整个拉回本地挑。首次检测一个账号时始终走全量，保证不漏掉已经发过的结果信。若站点换了发件人域、结果标题里又没有 Oasis，把它关掉。">
              <Switch size="small" checked={mailFilter}
                      onChange={changeMailFilter} />
            </Tooltip>
          </Space>
          <Button
            type="primary"
            icon={<CaretRightOutlined />}
            loading={busy}
            disabled={state?.running}
            onClick={() => run(
              () => api.start({ threads, interval, skip_hits: skipHits,
                                mail_filter: mailFilter,
                                only_opted: onlyOpted }),
              "已开始检测")}
          >
            开始检测
          </Button>
          <Button
            icon={<SyncOutlined />}
            loading={busy}
            onClick={() => run(() => api.start({ threads, interval }), "已排队再查一轮")}
          >
            立即检查一轮
          </Button>
          <Button
            danger
            icon={<PauseOutlined />}
            disabled={!state?.running}
            onClick={() => run(api.stop, "已请求停止（读完当前这批账号后停下）")}
          >
            停止
          </Button>
          {recommended && (
            <Tag color="blue" style={{ cursor: "pointer" }}
                 onClick={() => changeThreads(recommended)}>
              建议 {recommended} 并发
            </Tag>
          )}
        </Space>
        <div style={{ marginTop: 10 }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            只有 <Text strong>注册截止之后</Text> 收到的 Oasis 来信才算中签 ——
            截止前的信（含那封 Registration Complete）只证明预约成功。
            「已中签 0」在没有结果信之前是正常值。
            <br />
            检测只读邮箱，不向站点发任何请求。
            {host.total_mb
              ? ` 内存 ${host.total_mb - host.available_mb} / ${host.total_mb} MB 在用 · 可用 ${host.available_mb} MB · ${host.cores} 核 —— ${host.reason}`
              : ""}
          </Text>
        </div>
      </Card>

      <LogPanel />
    </>
  );
}
