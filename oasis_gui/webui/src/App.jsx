import { useEffect, useState, useCallback } from "react";
import { Layout, Menu, Button, Space, Typography, Tag, App as AntApp } from "antd";
import {
  DashboardOutlined, MailOutlined, GlobalOutlined, ScheduleOutlined,
  DatabaseOutlined, SettingOutlined, LogoutOutlined,
} from "@ant-design/icons";
import { api, AuthError } from "./api.js";
import Login from "./Login.jsx";
import Dashboard from "./pages/Dashboard.jsx";
import Mailboxes from "./pages/Mailboxes.jsx";
import Proxies from "./pages/Proxies.jsx";
import Registrations from "./pages/Registrations.jsx";
import Database from "./pages/Database.jsx";
import Settings from "./pages/Settings.jsx";

const { Sider, Content, Header } = Layout;
const { Text } = Typography;

const PAGES = [
  { key: "dash", icon: <DashboardOutlined />, label: "仪表盘" },
  { key: "mail", icon: <MailOutlined />, label: "邮箱池" },
  { key: "proxy", icon: <GlobalOutlined />, label: "代理池" },
  { key: "reg", icon: <ScheduleOutlined />, label: "预约记录" },
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
          <Text strong style={{ fontSize: 15 }}>Oasis Live &apos;27 注册控制台</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            浏览器真实 captcha · 官方表单驱动 · SQLite 去重落库
          </Text>
          <div style={{ flex: 1 }} />
          {state && (
            <Space size={8}>
              <Tag color={state.running ? "processing" : "default"}>
                {state.running ? "运行中" : "空闲"}
              </Tag>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {state.state} · 线程 {state.threads ?? "-"}
                {state.rounds ? ` · 第 ${state.rounds} 轮` : ""}
              </Text>
            </Space>
          )}
        </Header>

        <Content style={{ padding: 16, background: "#f5f5f5" }}>
          {tab === "dash" && <Dashboard state={state} refresh={refresh} />}
          {tab === "mail" && <Mailboxes state={state} refresh={refresh} />}
          {tab === "proxy" && <Proxies />}
          {tab === "reg" && <Registrations />}
          {tab === "data" && <Database refresh={refresh} />}
          {tab === "set" && <Settings state={state} refresh={refresh} />}
        </Content>
      </Layout>
    </Layout>
  );
}
