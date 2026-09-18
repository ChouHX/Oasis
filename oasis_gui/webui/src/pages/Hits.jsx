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
              { value: "success-mail", label: "成功邮件" },
              { value: "oasis-mail", label: "Oasis 来信" },
              { value: "site-ok", label: "站点已确认" },
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
        「站点已确认」这一类来自上一版程序的记录 —— confirm 回了 OK 却没发成功邮件
        （以及浏览器模式里页面确认即成功、从不发信）。活动已经结束，那封信不会再来，
        所以它们与收到成功邮件的账号进同一张名单，而不是被「没有成功邮件」吞掉。
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
