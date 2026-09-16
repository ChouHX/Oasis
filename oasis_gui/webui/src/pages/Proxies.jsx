import { useCallback, useEffect, useState } from "react";
import {
  Card, Table, Button, Space, Input, Typography, Tag, Alert, App as AntApp,
} from "antd";
import { ReloadOutlined, SaveOutlined } from "@ant-design/icons";
import { api } from "../api.js";

const { Text, Paragraph } = Typography;

// A proxy url carries user:pass. The server already masks it before it reaches
// the browser; this is a second belt so a raw value can never render by accident.
const mask = (u) => String(u || "").replace(/\/\/[^@/]+@/, "//***:***@");

export default function Proxies() {
  const { message } = AntApp.useApp();
  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [text, setText] = useState("");
  const [loading, setLoading] = useState(false);
  const [dirty, setDirty] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const d = await api.proxies();
      setRows(d.rows || []);
      setTotal(d.total || 0);
      setText((prev) => (dirty ? prev : (d.lines || []).join("\n")));
    } catch (e) {
      message.error(String(e.message || e));
    } finally {
      setLoading(false);
    }
  }, [dirty]);

  useEffect(() => { load(); }, [load]);

  const save = async () => {
    const n = text.split("\n").filter((x) => x.trim() && !x.trim().startsWith("#")).length;
    if (!n) { message.warning("至少要有一行"); return; }
    try {
      const d = await api.saveProxies(text);
      message.success(`已保存 ${d.total} 行，立即生效`);
      setDirty(false);
      setText((prev) => prev);
      load();
    } catch (e) {
      message.error(String(e.message || e));
    }
  };

  const columns = [
    { title: "代理", dataIndex: "url", ellipsis: true, render: mask },
    { title: "成功", dataIndex: "ok", width: 80, render: (v) => v ?? 0 },
    { title: "失败", dataIndex: "fail", width: 80, render: (v) => v ?? 0 },
    {
      title: "冷却中", dataIndex: "cooling", width: 90,
      render: (v) => (v ? <Tag color="warning">是</Tag> : ""),
    },
    {
      title: "最后错误", dataIndex: "last_error", ellipsis: true,
      render: (v) => <Text type="secondary" style={{ fontSize: 12 }}>{v || ""}</Text>,
    },
  ];

  return (
    <>
      <Card
        title="代理池配置"
        size="small"
        style={{ marginBottom: 12 }}
        extra={
          <Button type="primary" icon={<SaveOutlined />} onClick={save}>
            保存并生效
          </Button>
        }
      >
        <Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 8 }}>
          每行一个：<Text code>http://user:pass@host:port</Text> ·{" "}
          <Text code>socks5://user:pass@host:port</Text> ·{" "}
          <Text code>host:port</Text>。链路写法{" "}
          <Text code>前置代理|上游</Text>。保存后立即生效并写入配置文件，重启不会丢。
        </Paragraph>
        <Input.TextArea
          rows={10}
          value={text}
          onChange={(e) => { setText(e.target.value); setDirty(true); }}
          placeholder="每行一个代理…"
          style={{ fontFamily: "Consolas, Menlo, monospace", fontSize: 12 }}
        />
        <Alert
          style={{ marginTop: 10 }}
          type="info"
          showIcon
          message="保存时会用输入框里的内容整体替换。凭据加载时是遮罩的，不动就原样保存 —— 要改动请整行重写。"
        />
      </Card>

      <Card
        title="代理健康度"
        size="small"
        extra={
          <Space>
            <Text type="secondary" style={{ fontSize: 12 }}>共 {total} 个</Text>
            <Button icon={<ReloadOutlined />} onClick={load}>刷新</Button>
          </Space>
        }
      >
        <Table
          rowKey="url"
          size="small"
          loading={loading}
          columns={columns}
          dataSource={rows}
          scroll={{ x: 700 }}
          pagination={{ pageSize: 20, showSizeChanger: true }}
        />
      </Card>
    </>
  );
}
