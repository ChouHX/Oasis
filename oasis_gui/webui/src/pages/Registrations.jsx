import { useCallback, useEffect, useState } from "react";
import { Card, Table, Button, Typography, Tag, App as AntApp } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { api, STATUS } from "../api.js";

const { Text } = Typography;

export default function Registrations() {
  const { message } = AntApp.useApp();
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const d = await api.registrations();
      setRows(d.rows || []);
    } catch (e) {
      message.error(String(e.message || e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  // `shows` is translated server-side from the stored poll uuids, so the page
  // never has to know them.
  const columns = [
    { title: "账号", dataIndex: "email", ellipsis: true },
    {
      title: "场次（按偏好顺序）", dataIndex: "shows", width: 360,
      render: (v) => v || <Text type="secondary">—</Text>,
    },
    { title: "模式", dataIndex: "mode", width: 100 },
    {
      title: "状态", dataIndex: "status", width: 120,
      render: (v) => <Tag color={v === "submitted" ? "cyan" : "default"}>
        {STATUS[v]?.label || v}
      </Tag>,
    },
    {
      title: "时间", dataIndex: "created_at", width: 180,
      render: (v) => <Text type="secondary" style={{ fontSize: 12 }}>{v}</Text>,
    },
  ];

  return (
    <Card
      title="预约记录"
      size="small"
      extra={
        <Button icon={<ReloadOutlined />} onClick={load}>刷新</Button>
      }
    >
      <Table
        rowKey="id"
        size="small"
        loading={loading}
        columns={columns}
        dataSource={rows}
        scroll={{ x: 900 }}
        pagination={{ pageSize: 20, showSizeChanger: true }}
        locale={{ emptyText: "还没有记录" }}
      />
    </Card>
  );
}
