import { useEffect, useState, useCallback } from "react";
import { Layout, Menu, Button, Space, Typography, Tag, App as AntApp } from "antd";
import {
  DashboardOutlined, MailOutlined, TrophyOutlined, ScheduleOutlined,
  DatabaseOutlined, SettingOutlined, LogoutOutlined,
} from "@ant-design/icons";
import { api, AuthError } from "./api.js";
import Login from "./Login.jsx";
import Dashboard from "./pages/Dashboard.jsx";
import Mailboxes from "./pages/Mailboxes.jsx";
import Hits from "./pages/Hits.jsx";
import Registrations from "./pages/Registrations.jsx";
import Database from "./pages/Database.jsx";
import Settings from "./pages/Settings.jsx";

const { Sider, Content, Header } = Layout;
const { Text } = Typography;

const PAGES = [
  { key: "dash", icon: <DashboardOutlined />, label: "仪表盘" },
  { key: "mail", icon: <MailOutlined />, label: "邮箱池" },
  { key: "hits", icon: <TrophyOutlined />, label: "中签名单" },
  { key: "reg", icon: <ScheduleOutlined />, label: "旧预约" },
  { key: "data", icon: <DatabaseOutlined />, label: "数据库" },
];

export default function App() {
  const { message } = AntApp.useApp();
  const [auth, setAuth] = useState("checking");   // checking | in | out
  const [tab, setTab] = useState("dash");
  const [state, setState] = useState(null);

  // One poller for the whole app: the header, the dashboard cards and the
  // status pill all read from this, so they can never disagree.
  const refresh = useCallback(async () => {
    try {
      setState(await api.state());
      setAuth("in");
    } catch (e) {
      if (e instanceof AuthError) setAuth("out");
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 3000);
    return () => clearInterval(t);
  }, [refresh]);

  if (auth === "checking") return null;
  if (auth === "out") return <Login onDone={refresh} />;

  const post = (fn, ok) =>
    fn().then(() => { if (ok) message.success(ok); refresh(); })
         .catch((e) => message.error(String(e.message || e)));

  const started = !!state?.running;

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Sider width={208} theme="light" breakpoint="lg" collapsedWidth={64}
             style={{ borderRight: "1px solid #f0f0f0" }}>
        <div style={{ padding: "18px 16px 12px", fontWeight: 600, fontSize: 16 }}>
          Oasis 控制台
        </div>
        <Menu
          mode="inline"
          selectedKeys={[tab]}
          onClick={(e) => setTab(e.key)}
          items={PAGES}
          style={{ borderInlineEnd: "none" }}
        />
        <div style={{ position: "absolute", bottom: 0, width: "100%" }}>
          <Menu
            mode="inline"
            selectable={false}
            onClick={() => setTab("set")}
            items={[{ key: "set", icon: <SettingOutlined />, label: "设置" }]}
            style={{ borderInlineEnd: "none" }}
          />
          <div style={{ padding: "8px 16px 16px" }}>
            <Button
              block
              icon={<LogoutOutlined />}
              onClick={() => post(api.logout)}
            >
              退出登录
            </Button>
          </div>
        </div>
      </Sider>

      <Layout>
        <Header style={{
          background: "#fff", borderBottom: "1px solid #f0f0f0",
          padding: "0 20px", display: "flex", alignItems: "center", gap: 12,
        }}>
          <Text strong style={{ fontSize: 15 }}>Oasis Live &apos;27 中签检测台</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            定时巡检已导入账号的收件箱 · 邮件证据与站点记录合并判定
          </Text>
          <div style={{ flex: 1 }} />
          {state?.build && (
            // 服务端的 build 指纹。部署里 pull 没生效、容器没重建、浏览器拿
            // 缓存——三种情况界面长得一模一样，而修法各不相同；这一行让页面
            // 自己说清它连的是哪一版。与 `curl /health` 的 build 应当一致。
            <Text type="secondary"
                  style={{ fontSize: 11, fontFamily: "monospace" }}
                  title={state.build}>
              {String(state.build).split(" ")[0]}
            </Text>
          )}
          {state && (
            <Space size={8}>
              <Tag color={state.running ? "processing" : "default"}>
                {state.running ? "巡检中" : "空闲"}
              </Tag>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {state.state} · 并发 {state.threads ?? "-"} · 间隔 {state.interval ?? "-"}s
                {state.sweeps ? ` · 第 ${state.sweeps} 轮` : ""}
                {state.stats?.hits !== undefined ? ` · 中签 ${state.stats.hits}` : ""}
              </Text>
            </Space>
          )}
        </Header>

        <Content style={{ padding: 16, background: "#f5f5f5" }}>
          {tab === "dash" && <Dashboard state={state} refresh={refresh} />}
          {tab === "mail" && <Mailboxes state={state} refresh={refresh} />}
          {tab === "hits" && <Hits refresh={refresh} />}
          {tab === "reg" && <Registrations />}
          {tab === "data" && <Database refresh={refresh} />}
          {tab === "set" && <Settings state={state} refresh={refresh} />}
        </Content>
      </Layout>
    </Layout>
  );
}
