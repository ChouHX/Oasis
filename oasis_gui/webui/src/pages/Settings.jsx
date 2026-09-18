import { useEffect, useRef, useState } from "react";
import {
  Card, Form, Input, Button, Select, Space, Typography, Alert, App as AntApp, Tag,
} from "antd";
import { SaveOutlined, UndoOutlined } from "@ant-design/icons";
import { api } from "../api.js";

const { Text, Paragraph } = Typography;

// Mirrors the settings page of the desktop console, same order and same labels,
// so the two never drift. Concurrency, interval, the look-back window and the
// skip-hits switch live on the dashboard in both.
const FIELDS = [
  { key: "per_page", label: "每箱取信（封）", type: "int",
    hint: "每个邮箱取最近多少封信来判断。判据是「Oasis 来信」本身，" +
          "所以不需要把整箱读完。" },
  { key: "mail_proxy", label: "取件代理", type: "text", wide: true,
    hint: "留空 = 直连（推荐）。仅在网络必须走代理时才填。" },
  { key: "hme_base", label: "iCloud 服务地址", type: "text", wide: true,
    hint: "容器里要用 host.docker.internal，不是 127.0.0.1。" },
  { key: "hme_password", label: "iCloud 密码", type: "password", wide: true },
  { key: "alias_inbox", label: "别名收件箱（Gmail）", type: "text", wide: true,
    hint: "iCloud 隐私邮箱只是转发地址：发给它的信会落到 Apple ID 绑定的那个 Gmail 里，" +
          "所以一个收件箱覆盖整份别名列表。填 Gmail 地址后，导入页可以直接粘贴纯别名。" },
  { key: "alias_inbox_password", label: "Gmail 应用专用密码", type: "password", wide: true,
    hint: "不是 Google 登录密码。在 Gmail 设置里生成应用专用密码，并确认已开启 IMAP。" },
  { key: "db_path", label: "数据库路径（只读）", type: "readonly", wide: true },
  { key: "debug", label: "调试堆栈", type: "bool" },
];

export default function Settings({ state, refresh }) {
  const { message } = AntApp.useApp();
  const [form] = Form.useForm();
  const [busy, setBusy] = useState(false);
  // What the server last reported, kept so "撤销改动" can put those values back
  // instead of clearing the form.
  const server = useRef({});
  const seeded = useRef(false);

  useEffect(() => {
    const c = state?.config;
    if (!c) return;
    const init = {};
    for (const f of FIELDS) {
      const v = c[f.key];
      if (Array.isArray(v)) init[f.key] = v.join(",");
      else if (f.type === "bool") init[f.key] = v ? "true" : "false";
      else init[f.key] = v ?? "";
    }
    server.current = init;
    // Seed the form once per visit, never again.
    //
    // The shell polls /api/state every three seconds and every reply is a fresh
    // object, so an effect keyed on it re-ran on every poll and wrote the
    // server's values back over whatever was being typed - the settings form
    // was effectively read-only. Seeding once leaves the form alone while
    // someone is using it; leaving the page and coming back reseeds.
    if (seeded.current) return;
    seeded.current = true;
    form.setFieldsValue(init);
  }, [state?.config, form]);

  const save = async () => {
    setBusy(true);
    try {
      const values = await form.validateFields();
      const patch = {};
      for (const f of FIELDS) {
        if (f.type === "readonly") continue;
        let v = values[f.key];
        if (f.type === "int") v = parseInt(v, 10);
        if (f.type === "float") v = parseFloat(v);
        if (f.type === "bool") v = String(v) === "true";
        patch[f.key] = v;
      }
      await api.saveConfig(patch);
      message.success("已保存，下一轮生效");
      refresh();
    } catch (e) {
      if (e?.errorFields) return;             // form validation already showed
      message.error(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card
      title="设置"
      size="small"
      extra={
        <Space>
          <Button icon={<UndoOutlined />}
                  onClick={() => form.setFieldsValue(server.current)}>
            撤销改动
          </Button>
          <Button type="primary" icon={<SaveOutlined />} loading={busy} onClick={save}>
            保存设置
          </Button>
        </Space>
      }
    >
      <Paragraph type="secondary" style={{ fontSize: 12 }}>
        改动会写回服务端配置文件；并发与间隔在下一轮巡检开始时读取。
        账号在「邮箱池」管理，中签结果在「中签名单」。
      </Paragraph>

      <Form form={form} layout="vertical" size="small">
        {FIELDS.map((f) => (
          <Form.Item key={f.key} name={f.key} label={f.label}
                     style={{ maxWidth: f.wide ? 720 : 320, marginBottom: 12 }}>
            {f.type === "bool" ? (
              <Select
                options={[{ value: "true", label: "开" }, { value: "false", label: "关" }]}
              />
            ) : f.type === "password" ? (
              <Input.Password />
            ) : f.type === "readonly" ? (
              <Input disabled />
            ) : (
              <Input />
            )}
          </Form.Item>
        ))}
      </Form>

      <Alert
        type="info"
        showIcon
        message="检测不会向站点发任何请求"
        description={
          <span>
            程序只读邮箱。旧版程序里「重复向站点请求验证邮件会作废会话」这件事在这里
            不可能发生 —— 没有任何代码路径会碰站点。<Text code>取件代理</Text>{" "}
            只影响 IMAP / Graph 的连接方式。
          </span>
        }
      />

      {state?.host?.total_mb && (
        <div style={{ marginTop: 12 }}>
          <Space size={6}>
            <Tag>本机</Tag>
            <Text type="secondary" style={{ fontSize: 12 }}>
              内存 {state.host.total_mb - state.host.available_mb} /{" "}
              {state.host.total_mb} MB 在用 · 可用 {state.host.available_mb} MB ·{" "}
              {state.host.cores} 核 · 建议 {state.host.recommended} 并发
            </Text>
          </Space>
        </div>
      )}
    </Card>
  );
}
