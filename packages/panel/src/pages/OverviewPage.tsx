import type { Overview, Page } from '../types';
import { date, Details, formatBytes, Section, Status } from '../components/common';
import { Icon, type IconName } from '../components/Icon';

function Resource({className, icon, title, value, detail, onClick}: {className: string; icon: IconName; title: string; value: string; detail: string; onClick: () => void}) {
  return <button className={`resource ${className}`} onClick={onClick}><span className="resource-icon"><Icon name={icon} /></span><span className="resource-type">{title}</span><strong>{value}</strong><span className="resource-detail">{detail}</span><Icon name="arrow" className="resource-arrow" /></button>;
}
export function OverviewPage({data, navigate}: {data: Overview; navigate: (page: Page) => void}) {
  const active = data.services.filter(item => item.status === 'active').length;
  return <>
    <section className="topology-section" aria-labelledby="topology-title">
      <div className="section-head"><div><h2 id="topology-title">服务依赖与发布路径</h2><p>网站、证书、代理节点与客户端订阅。点击资源进入管理。</p></div><span className="revision">版本 {data.revision}</span></div>
      <div className="topology">
        <div className="host-strip"><Icon name="server" /><button onClick={() => navigate('server')}>{data.server.hostname}</button><span>{data.server.os}</span><span className="host-status">{active} / {data.services.length} 项服务运行</span></div>
        <svg className="topology-lines" viewBox="0 0 900 300" preserveAspectRatio="none" aria-hidden="true"><path d="M250 80H315Q335 80 335 100V150H375M250 240H315Q335 240 335 220V150H375M575 150H640" /><path className="topology-dots" d="M250 80H315Q335 80 335 100V150H375M575 150H640" /></svg>
        <Resource className="resource-site" icon="site" title="受管网站" value={data.website.domain} detail="普通 HTTPS 访问 · 本机握手目标" onClick={() => navigate('site')} />
        <Resource className="resource-certificate" icon="lock" title="TLS 证书" value={data.certificate.automatic_renewal ? 'HTTP-01 自动续期' : '外部管理证书'} detail={`有效期至 ${date(data.certificate.not_after).split(' ')[0]}`} onClick={() => navigate('site')} />
        <Resource className="resource-proxy" icon="proxy" title={data.proxy.protocol} value={data.proxy.name} detail={`TCP ${data.proxy.port} · 当前 SNI ${data.proxy.sni}`} onClick={() => navigate('proxy')} />
        <Resource className="resource-sub" icon="subscription" title="客户端订阅" value={data.subscription.name} detail={data.subscription.domain} onClick={() => navigate('subscription')} />
        <div className="topology-caption"><span><i className="connection-line" />依赖与发布关系</span><span>站点与订阅共享 TCP {data.proxy.port}，由 SNI 路由</span></div>
      </div>
    </section>
    {data.hy2.node && <Section title="HY2 发布关系" action={<button className="text-button" onClick={() => navigate('proxy')}>管理 HY2<Icon name="arrow" /></button>}><Details rows={[
      ['节点', data.hy2.node.name], ['状态', data.hy2.node.enabled ? '已启用，发布至当前订阅' : '已停用，保留配置与证书依赖'],
      ['独立监听', `UDP ${data.hy2.node.port}`], ['TLS 服务名', data.hy2.node.server_name], ['证书绑定', data.hy2.node.certificate_id],
    ]} /></Section>}
    {data.nodes.nodes.length > 0 && <Section title="独立协议节点" description="启用的节点加入现有订阅，使用独立公网端口。" action={<button className="text-button" onClick={() => navigate('proxy')}>管理节点<Icon name="arrow" /></button>}><Details rows={data.nodes.nodes.map(node => [node.name, `${node.protocol.toUpperCase()} · ${node.port} · ${node.enabled ? '已发布' : '已停用'}`])} /></Section>}
    <div className="two-column overview-lower">
      <Section title="服务状态" description="来自当前系统服务状态"><div className="service-list">{data.services.map(service => <div key={service.name}><span><Icon name="server" /><code>{service.name}</code></span><Status value={service.status} /></div>)}</div></Section>
      <Section title="部署信息" action={<button className="text-button" onClick={() => navigate('server')}>服务器详情<Icon name="arrow" /></button>}><Details rows={[
        ['架构', data.server.architecture], ['物理内存', formatBytes(data.server.memory_total_bytes)],
        ['流量信息来源', data.subscription.traffic_source === 'manual' ? '手工填写' : data.subscription.traffic_source],
        ['代理验证记录', <Status value={data.proxy.verification} />],
      ]} /></Section>
    </div>
    <Section title="最近任务" action={<button className="text-button" onClick={() => navigate('jobs')}>查看全部<Icon name="arrow" /></button>}>
      {data.recent_jobs.length ? <div className="recent-jobs">{data.recent_jobs.slice(0, 4).map(job => <button className="recent-job" key={job.id} onClick={() => navigate('jobs')}><span><strong>{job.operation}</strong><small>{date(job.created_at)}</small></span><Status value={job.status} /><Icon name="arrow" /></button>)}</div> : <p className="empty">还没有面板任务。首次预览或应用变更后，记录会显示在这里。</p>}
    </Section>
  </>;
}
