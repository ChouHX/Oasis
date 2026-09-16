import { useState } from "react";
import { Card, Form, Input, Button, Typography, Alert } from "antd";
import { LockOutlined } from "@ant-design/icons";
import { api } from "./api.js";

const { Title, Text } = Typography;

export default function Login({ onDone }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [form] = Form.useForm();

  const submit = async ({ password }) => {
    setBusy(true);
    setError("");
    try {
      await api.login(password);
      onDone();
    } catch (e) {
      setError(e.message === "RATE_LIMITED"
        ? "尝试次数过多，请稍后再试"
        : "密码错误");
      form.setFieldValue("password", "");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{
      minHeight: "100vh", display: "flex", alignItems: "center",
      justifyContent: "center", background: "#f5f5f5",
    }}>
      <Card style={{ width: 340 }}>
        <Title level={4} style={{ marginTop: 0, marginBottom: 4 }}>Oasis 控制台</Title>
        <Text type="secondary">需要密码才能访问</Text>
        <Form form={form} layout="vertical" onFinish={submit} style={{ marginTop: 16 }}>
          <Form.Item name="password" rules={[{ required: true, message: "请输入密码" }]}>
            <Input.Password
              prefix={<LockOutlined />}
              placeholder="管理密码"
              autoFocus
              size="large"
            />
          </Form.Item>
          {error && (
            <Alert type="error" message={error} showIcon style={{ marginBottom: 12 }} />
          )}
          <Button type="primary" htmlType="submit" block size="large" loading={busy}>
            进入
          </Button>
        </Form>
      </Card>
    </div>
  );
}
