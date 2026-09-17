import { useCallback, useEffect, useState } from "react";
import {
  Card, Table, Button, Space, Select, Input, Tag, Typography, Alert,
  Checkbox, Empty, Popconfirm, App as AntApp,
} from "antd";
import {
  ReloadOutlined, DownloadOutlined, UploadOutlined, CloudDownloadOutlined,
} from "@ant-design/icons";
import { api, STATUS, STATUS_FILTER } from "../api.js";

const { Text, Paragraph } = Typography;

const HINT = (
  <>
    每行一个，空行与 # 开头的行会忽略。
    <br />
    <Text code>outlook@x.com----密码----client_id----refresh_token</Text>
    <br />
    <Text code>gmail@x.com----密码----client_id----client_secret----refresh_token</Text>
    <br />
    <Text code>别名@icloud.com----acc_xxxxxxxx----hme</Text>
    <br />
    <Text code>别名@icloud.com----gmail地址----应用专用密码----gmail-imap</Text>
    <br />
    iCloud 隐私邮箱走 Gmail 取件时，只要在「设置」里填好别名收件箱，
    <Text strong>直接粘贴纯别名列表就行</Text>
    （<Text code>别名@icloud.com</Text> 一行一个），收件箱会自己补进去。
  </>
);

function IcloudPanel({ onImported }) {
  const { message } = AntApp.useApp();
  const [aliases, setAliases] = useState(null);
  const [picked, setPicked] = useState([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const load = async () => {
    setBusy(true);
    setErr("");
    try {
      const d = await api.icloudAliases();
      setAliases(d.aliases || []);
      setPicked([]);
    } catch (e) {
      setErr(String(e.message || e));
      setAliases(null);
    } finally {
      setBusy(false);
    }
  };

  const doImport = async () => {
    if (!picked.length) { setErr("先勾选要导入的别名"); return; }
    setBusy(true);
    setErr("");
    try {
      const d = await api.icloudImport(picked);
      message.success(`导入 ${d.added} 个，已在池中 ${d.duplicate} 个`);
      load();
      onImported();
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };

  const selectable = (aliases || []).filter((a) => !a.imported);
  const importedCount = (aliases || []).filter((a) => a.imported).length;

  return (
    <Card
      title="iCloud 别名（勾选后导入）"
      size="small"
      style={{ marginBottom: 12 }}
      extra={
        <Space>
          <Button icon={<CloudDownloadOutlined />} loading={busy} onClick={load}>
            拉取别名列表
          </Button>
          <Button disabled={!aliases} onClick={() => setPicked([])}>全不选</Button>
          <Button
            disabled={!aliases}
            onClick={() => setPicked(selectable.map((a) => a.email))}
          >
            只选未导入的
          </Button>
          <Button type="primary" loading={busy} disabled={!picked.length}
                  onClick={doImport}>
            导入勾选项{picked.length ? ` (${picked.length})` : ""}
          </Button>
        </Space>
      }
    >
      <Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 10 }}>
        iCloud 建别名有配额（约每小时 10 个），所以没有全选 ——
        <Text strong>只导入你真正要用的</Text>。已导入的会置灰。
        服务地址和密码在「设置」里配。
      </Paragraph>

      {err && <Alert type="error" message={err} showIcon style={{ marginBottom: 10 }} />}

      {aliases === null ? (
        <Empty description="还没有拉取" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      ) : aliases.length === 0 ? (
        <Empty description="服务上没有别名" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      ) : (
        <>
          <Text type="secondary" style={{ fontSize: 12 }}>
            共 {aliases.length} 个，已导入 {importedCount} 个
          </Text>
          <Checkbox.Group
            style={{ display: "block", marginTop: 8 }}
            value={picked}
            onChange={setPicked}
          >
            <Space direction="vertical" size={2} style={{ width: "100%" }}>
              {aliases.map((a) => (
                <Checkbox key={a.email} value={a.email} disabled={a.imported}>
                  <Space size={8}>
                    <Text style={{ minWidth: 210, display: "inline-block" }}>
                      {a.email}
                    </Text>
                    <Tag color={a.active ? "success" : "error"}>
                      {a.active ? "启用" : "停用"}
                    </Tag>
                    {a.account_name && <Text type="secondary">{a.account_name}</Text>}
                    {a.label && <Text type="secondary">{a.label}</Text>}
                    {a.imported && <Text type="secondary">（已在池中）</Text>}
                  </Space>
                </Checkbox>
              ))}
            </Space>
          </Checkbox.Group>
        </>
      )}
    </Card>
  );
}

function ImportPanel({ onImported }) {
  const { message } = AntApp.useApp();
  const [lines, setLines] = useState("");
  const [protocol, setProtocol] = useState("auto");
  const [busy, setBusy] = useState(false);

  const doImport = async () => {
    if (!lines.trim()) { message.warning("先粘贴内容"); return; }
    setBusy(true);
    try {
      const d = await api.importAccounts(lines, protocol);
      message.success(`新增 ${d.added} 条，重复 ${d.duplicate} 条`);
      setLines("");
      onImported();
    } catch (e) {
      message.error(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card title="手动导入账号" size="small" style={{ marginBottom: 12 }}>
      <Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 8 }}>
        {HINT}
      </Paragraph>
      <Input.TextArea
        rows={5}
        value={lines}
        onChange={(e) => setLines(e.target.value)}
        placeholder="粘贴凭据行…"
        style={{ fontFamily: "Consolas, Menlo, monospace", fontSize: 12 }}
      />
      <Space style={{ marginTop: 10 }}>
        <Text type="secondary">协议</Text>
        <Select
          value={protocol}
          onChange={setProtocol}
          style={{ width: 190 }}
          options={[
            { value: "auto", label: "自动" },
            { value: "graph", label: "Microsoft Graph" },
            { value: "imap", label: "IMAP" },
            { value: "hme", label: "iCloud HME" },
            { value: "alias-imap", label: "iCloud 别名（Gmail 取件）" },
          ]}
        />
        <Button type="primary" icon={<UploadOutlined />} loading={busy}
                onClick={doImport}>
          导入到数据库
        </Button>
      </Space>
    </Card>
  );
}

export default function Mailboxes({ refresh }) {
  const { message } = AntApp.useApp();
  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [per, setPer] = useState(50);
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(false);

  const load = useCallback(async (p = page) => {
    setLoading(true);
    try {
      const d = await api.accounts(status, p);
      setRows(d.rows || []);
      setTotal(d.total || 0);
      setPage(d.page || 1);
      setPer(d.per || 50);
    } catch (e) {
      message.error(String(e.message || e));
    } finally {
      setLoading(false);
    }
  }, [status, page]);

  useEffect(() => { load(1); /* eslint-disable-next-line */ }, [status]);

  const refreshAll = () => { load(page); refresh(); };

  const reset = async () => {
    const d = await api.resetAccounts();
    message.success(`重置 ${d.changed} 条`);
    refreshAll();
  };

  const columns = [
    { title: "ID", dataIndex: "id", width: 70 },
    { title: "邮箱", dataIndex: "email", ellipsis: true },
    { title: "协议", dataIndex: "protocol", width: 90 },
    {
      title: "状态", dataIndex: "status", width: 120,
      render: (v) => <Tag color={v === "registered" ? "success"
        : v === "submitted" ? "cyan"
        : v === "failed" ? "error"
        : v === "running" ? "processing" : "default"}>
        {STATUS[v]?.label || v}
      </Tag>,
    },
    {
      title: "姓名", width: 140,
      render: (_, r) => `${r.first_name || ""} ${r.last_name || ""}`.trim() || "—",
    },
    { title: "电话", dataIndex: "phone", width: 130 },
    {
      title: "备注", dataIndex: "error", ellipsis: true,
      render: (v) => <Text type="secondary" style={{ fontSize: 12 }}>{v || ""}</Text>,
    },
  ];

  return (
    <>
      <IcloudPanel onImported={refreshAll} />
      <ImportPanel onImported={refreshAll} />

      <Card
        title="账号池"
        size="small"
        extra={
          <Space>
            <Text type="secondary" style={{ fontSize: 12 }}>共 {total} 条</Text>
            <Select value={status} onChange={setStatus} style={{ width: 150 }}
                    options={STATUS_FILTER} />
            <Button icon={<ReloadOutlined />} onClick={() => load(1)}>刷新</Button>
            <Popconfirm title="把失败与卡住的账号放回队列？" onConfirm={reset}
                        okText="确认" cancelText="取消">
              <Button>重置失败与卡住项</Button>
            </Popconfirm>
            <Button icon={<DownloadOutlined />}
                    onClick={() => { window.location.href = api.exportUrl("creds"); }}>
              导出全部凭据
            </Button>
          </Space>
        }
      >
        <Table
          rowKey="id"
          size="small"
          loading={loading}
          columns={columns}
          dataSource={rows}
          scroll={{ x: 900 }}
          pagination={{
            current: page,
            pageSize: per,
            total,
            showSizeChanger: false,
            onChange: (p) => load(p),
          }}
        />
      </Card>
    </>
  );
}
