import { useState } from "react";
import {
  Card, Button, Space, Select, Typography, Alert, App as AntApp, Popconfirm,
} from "antd";
import { DeleteOutlined, DownloadOutlined, CompressOutlined } from "@ant-design/icons";
import { api, STATUS } from "../api.js";

const { Text, Paragraph } = Typography;

export default function Database({ refresh }) {
  const { message } = AntApp.useApp();
  const [target, setTarget] = useState("pending");
  const [busy, setBusy] = useState(false);

  const guard = async (fn, ok) => {
    setBusy(true);
    try {
      const d = await fn();
      if (ok) message.success(ok(d));
      refresh?.();
    } catch (e) {
      message.error(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card title="数据库维护" size="small">
      <Alert
        type="warning"
        showIcon
        style={{ marginBottom: 12 }}
        message="清理不可撤销"
        description="账号被删除时，它的旧预约记录会一并删除（外键级联）。"
      />

      <Space wrap size={12}>
        <Button
          icon={<CompressOutlined />}
          loading={busy}
          onClick={() => guard(api.vacuum, () => "已压缩")}
        >
          VACUUM 压缩
        </Button>
        <Button
          icon={<DownloadOutlined />}
          onClick={() => { window.location.href = api.exportUrl("hits"); }}
        >
          导出中签 CSV
        </Button>
        <Button
          onClick={() => { window.location.href = api.exportUrl("hit_creds"); }}
        >
          导出中签凭据
        </Button>
        <Button
          icon={<DownloadOutlined />}
          onClick={() => { window.location.href = api.exportUrl("accounts"); }}
        >
          导出账号 JSON
        </Button>
      </Space>

      <Paragraph type="secondary" style={{ fontSize: 12, margin: "16px 0 8px" }}>
        按类批量清理（「未中签」= 还没中签的账号，检测记录会随账号一起删掉）：
      </Paragraph>
      <Space wrap>
        <Select
          value={target}
          onChange={setTarget}
          style={{ width: 190 }}
          options={[
            { value: "pending", label: "未中签" },
            { value: "hit", label: "已中签" },
          ]}
        />
        <Popconfirm
          title={`永久删除所有「${STATUS[target]?.label || target}」账号？`}
          description="不可撤销，旧的预约记录会一并删除。"
          okText="确认删除"
          okButtonProps={{ danger: true }}
          cancelText="取消"
          onConfirm={() => guard(() => api.deleteAccounts(target),
                                 (d) => `已删除 ${d.deleted} 条`)}
        >
          <Button danger icon={<DeleteOutlined />} loading={busy}>清理该状态</Button>
        </Popconfirm>
        <Text type="secondary" style={{ fontSize: 12 }}>
          账号池与中签数请见仪表盘 —— 这里的计数以服务端为准。
        </Text>
      </Space>
    </Card>
  );
}
