import { useEffect, useRef, useState } from "react";
import {
  Card, Table, Button, Typography, Tag, Space, App as AntApp, Segmented,
} from "antd";
import { ReloadOutlined, DownloadOutlined } from "@ant-design/icons";
import { api, SOURCE } from "../api.js";

const { Text, Paragraph } = Typography;

export default function Hits({ refresh }) {
  const { message } = AntApp.useApp();
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState("all");
  const timer = useRef(null);

  const load = async (quiet) => {
    if (!quiet) setLoading(true);
    try {
      const d = await api.hits();
      setRows(d.rows || []);
    } catch (e) {
      if (!quiet) message.error(String(e.message || e));
    } finally {
      if (!quiet) setLoading(false);
    }
  };

  useEffect(() => {
    load();
    // 中签名单是这张页面的全部内容，而一轮巡检可能要好几分钟 —— 每 10 秒
    // 自己拉一次，比要求操作者手动刷新更贴近「盯着结果」的实际用法。
    timer.current = setInterval(() => load(true), 10000);
    return () => clearInterval(timer.current);
  }, []);

  const shown = filter === "all"
    ? rows
    : rows.filter((r) => r.hit_source === filter);

  const columns = [
    { title: "邮箱", dataIndex: "email", ellipsis: true },
    {
      title: "证据来源", dataIndex: "hit_source", width: 190,
      render: (v, r) => (
        <Tag color={SOURCE[v]?.color || "default"}>{r.label || SOURCE[v]?.label || v}</Tag>
      ),
    },
    { title: "说明", dataIndex: "hit_note", ellipsis: true },
    { title: "最近来信", dataIndex: "last_mail_subject", ellipsis: true },
    {
      title: "来信时间", dataIndex: "last_mail_at", width: 150,
      render: (v) => <Text type="secondary" style={{ fontSize: 12 }}>{v || "—"}</Text>,
    },
    { title: "中签时间", dataIndex: "hit_at", width: 150 },
    {
      title: "协议", dataIndex: "protocol", width: 100,
      render: (v) => <Text type="secondary" style={{ fontSize: 12 }}>{v || "—"}</Text>,
    },
  ];

  return (
    <Card
      title={`中签名单（${rows.length}）`}
      size="small"
      extra={
        <Space>
          <Segmented
            size="small"
            value={filter}
            onChange={setFilter}
            options={[
              { value: "all", label: "全部" },
              { value: "oasis-mail", label: "结果信" },
            ]}
          />
          <Button icon={<ReloadOutlined />} onClick={() => load()}>刷新</Button>
          <Button
            icon={<DownloadOutlined />}
            onClick={() => { window.location.href = api.exportUrl("hits"); }}
          >
            导出 CSV
          </Button>
          <Button
            onClick={() => { window.location.href = api.exportUrl("hit_creds"); }}
          >
            导出凭据
          </Button>
        </Space>
      }
    >
      <Paragraph type="secondary" style={{ fontSize: 12 }}>
        名单里只会有<Text strong>注册截止之后</Text>收到的 Oasis 来信 —— 也就是结果
        通知。截止之前的信（验证信、Registration Complete）一律不算：它们证明的是
        「预约成功」，不是「中签」。所以名单空着是正常的，直到 Oasis 发结果。
      </Paragraph>
      <Table
        rowKey="id"
        size="small"
        loading={loading}
        columns={columns}
        dataSource={shown}
        scroll={{ x: 1080 }}
        pagination={{ pageSize: 20, showSizeChanger: true }}
        locale={{ emptyText: "还没有中签记录" }}
      />
    </Card>
  );
}
