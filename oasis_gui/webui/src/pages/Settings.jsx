import { useEffect, useState } from "react";
import {
  Card, Form, Input, Button, Space, Typography, Alert, App as AntApp, Tag,
} from "antd";
import { SaveOutlined, UndoOutlined } from "@ant-design/icons";
import { api } from "../api.js";

const { Text, Paragraph } = Typography;

// Mirrors the settings page of the desktop console. Proxies and accounts are
// deliberately absent - they have their own pages, and a proxy list does not
// belong in a text field on a form.
const FIELDS = [
  { key: "mode", label: "模式", type: "select", options: ["browser", "hybrid"] },
  { key: "threads", label: "线程数", type: "int" },
  { key: "shows", label: "场次偏好（逗号分隔）", type: "list" },
  { key: "link_timeout", label: "等邮件超时（秒）", type: "int" },
  { key: "success_timeout", label: "等成功邮件超时（秒）", type: "int" },
  { key: "verify_success", label: "成功后校验邮件", type: "bool" },
  { key: "front_proxy", label: "前置代理（链路）", type: "text", wide: true },
  { key: "google_proxy", label: "Google 分流代理", type: "text", wide: true },
  { key: "mail_proxy", label: "取件代理", type: "text", wide: true },
  { key: "hme_base", label: "iCloud 服务地址", type: "text", wide: true },
  { key: "hme_password", label: "iCloud 密码", type: "password", wide: true },
  { key: "db_path", label: "数据库路径（只读）", type: "readonly", wide: true },
];

export default function Settings({ state, refresh }) {
  const { message } = AntApp.useApp();
  const [form] = Form.useForm();
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const c = state?.config;
    if (!c) return;
    const init = {};
    for (const f of FIELDS) {
      const v = c[f.key];
      init[f.key] = Array.isArray(v) ? v.join(",") : (v ?? "");
    }
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
        if (f.type === "bool") v = String(v) === "true";
        if (f.type === "list") {
          v = String(v ?? "").split(",").map((x) => x.trim()).filter(Boolean);
        }
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
          <Button icon={<UndoOutlined />} onClick={() => form.resetFields()}>
            撤销改动
          </Button>
          <Button type="primary" icon={<SaveOutlined />} loading={busy} onClick={save}>
            保存设置
          </Button>
        </Space>
      }
    >
      <Paragraph type="secondary" style={{ fontSize: 12 }}>
        改动会写回服务端配置文件并立即生效（线程数与模式在下一轮开始时读取）。
        代理池和账号在各自页面管理。
      </Paragraph>

      <Form form={form} layout="vertical" size="small">
        {FIELDS.map((f) => (
          <Form.Item key={f.key} name={f.key} label={f.label}
                     style={{ maxWidth: f.wide ? 720 : 320, marginBottom: 12 }}>
            {f.type === "select" ? (
              <Input />
            ) : f.type === "password" ? (
              <Input.Password placeholder="启动时该服务的管理员密码" />
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
        message="浏览器模式必须能访问 Google"
        description={
          <span>
            reCAPTCHA 在 Google 上。没有配 <Text code>Google 分流代理</Text> 时，
            如果注册代理本身不通 Google，页面会卡在{" "}
            <Text code>wait_for_function</Text> 超时。
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
              {state.host.cores} 核 · 建议 {state.host.recommended} 线程
            </Text>
          </Space>
        </div>
      )}
    </Card>
  );
}
