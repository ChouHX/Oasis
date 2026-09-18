import { useCallback, useEffect, useState } from "react";
import { Card, Table, Button, Typography, Tag, App as AntApp } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { api } from "../api.js";

const { Text, Paragraph } = Typography;

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

  // 这一页是上一版程序留下的只读记录：检测程序不再写它。它的价值在于
  // 「这个账号当年预约了哪一场」—— 中签本身是场次级的事，没有它，中签名单
  // 就只剩邮箱地址。
  const columns = [
    { title: "账号", dataIndex: "email", ellipsis: true },
    { title: "当时的方式", dataIndex: "mode", width: 120 },
    {
      title: "记录状态", dataIndex: "status", width: 130,
      render: (v) => <Tag color={v === "submitted" ? "cyan" : "default"}>{v}</Tag>,
    },
    {
      title: "时间", dataIndex: "created_at", width: 180,
      render: (v) => <Text type="secondary" style={{ fontSize: 12 }}>{v}</Text>,
    },
  ];

  return (
    <Card
      title={`旧预约记录（${rows.length}）`}
      size="small"
      extra={<Button icon={<ReloadOutlined />} onClick={load}>刷新</Button>}
    >
      <Paragraph type="secondary" style={{ fontSize: 12 }}>
        上一版程序提交注册时留下的记录，只读。中签判定不看这张表 ——
        账号能不能读、中没中签，都由检测结果决定。
      </Paragraph>
      <Table
        rowKey="id"
        size="small"
        loading={loading}
        columns={columns}
        dataSource={rows}
        scroll={{ x: 760 }}
        pagination={{ pageSize: 20, showSizeChanger: true }}
        locale={{ emptyText: "还没有记录" }}
      />
    </Card>
  );
}
