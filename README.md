# 🏠 Homelab   
Under Construction - Formatting and relevent details taken from <a href="https://github.com/mischavandenburg/homelab/">Mischa van den Burg/Homelab</a>

## Introduction

This repo contains all of the configuration and documentation of my homelab.

The purpose of my homelab is to learn and to have fun. As a system administrator my homelab is the place where I can try out and learn new things. On the other hand, by self-hosting some applications, it makes me feel responsible for the entire process of deploying and maintaining an application from A to Z. It forces me to think about backup strategies, security, scalability and the ease of deployment and maintenance.

## Cluster Provisioning & Architecture

I use ProxMox & Portainer to to setup my machines and docker pods. I'm currently working on moving my entire lab into a kubernetes cluster. Initially with k3s and then Talos Linux.

## :computer: Hardware

### Nodes

I currently use an Intel NUC as well as a few Raspberry Pi's and my Synology NAS.

Intel NUC 8I7BEH i7-8559U/32GB/500GB SSD

Raspberry Pi5 8GB

Raspberry Pi4 2GB


## :rocket: Installed Apps & Tools

### Apps

End User Applications
<table>
    <tr>
        <th>Logo</th>
        <th>Name</th>
        <th>Description</th>
    </tr>
    <tr>
        <td><img width="32" src="https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/svg/commafeed.svg"></td>
        <td><a href="https://www.commafeed.com/#/welcome">Commafeed</a></td>
        <td>Bloat free RSS feed reader</td>
    </tr>
    <tr>
        <td><img width="32" src="https://www.svgrepo.com/download/499807/home-page.svg"></td>
        <td><a href="https://github.com/gethomepage/homepage">Homepage</a></td>
        <td>My customized portal to my homelab & internet</td>
    </tr>
    <tr>
        <td><img width="32" src="https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/svg/n8n.svg"></td>
        <td><a href="https://n8n.io/">n8n</a></td>
        <td>Secure, AI-native workflow automation</td>
    </tr>
    <tr>
        <td><img width="32" src="https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/svg/wallabag-light.svg"></td>
        <td><a href="https://wallabag.org/">Wallabag</a></td>
        <td>Save articles & posts from the web for storage & reading later</td>
    </tr>
</table>

### Infrastructure

Everything needed to run my cluster & deploy my applications
<table>
    <tr>
        <th>Logo</th>
        <th>Name</th>
        <th>Description</th>
    </tr>
    <tr>
        <td><img width="32" src="https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/cloudflare-zero-trust.png"></td>
        <td><a href="https://developers.cloudflare.com/cloudflare-one/">Cloudflare Zero Trust</a></td>
        <td>Used for private tunnels to expose public services (without requiring a public IP).</td>
    </tr>
    <tr>
        <td><img width="32" src="https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/svg/postgresql.svg"></td>
        <td><a href="https://cloudnative-pg.io/">CloudNativePG</a></td>
        <td>Database operator for running PostgreSQL clusters</td>
    </tr>
    <tr>
        <td><img width="32" src="https://www.svgrepo.com/download/477066/lock.svg"></td>
        <td><a href="https://external-secrets.io/latest/">External Secrets Operator</a></td>
        <td>Used to sync my secrets from Azure Key Vaults to my cluster</td>
    </tr>
    <tr>
        <td><img width="32" src="https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/svg/grafana.svg"></td>
        <td><a href="https://grafana.com/">Grafana</a></td>
        <td>The open observability platform.</td>
    </tr>
    <tr>
        <td><img width="32" src="https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/svg/prometheus.svg"></td>
        <td><a href="https://prometheus.io/">Prometheus</a></td>
        <td>An open-source monitoring system with a dimensional data model, flexible query language, efficient time series database and modern alerting approach.</td>
    </tr>
    <tr>
        <td><img width="32" src="https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/svg/synology.svg"></td>
        <td><a href="https://github.com/SynologyOpenSource/synology-csi">Synology CSI Driver</a></td>
        <td>Used to provision Persistent Volumes directly on my Synology</td>
    </tr>
</table>

## Networking

<TBA>

### Storage

I use a Synology DS923+ as a NAS. I use the Synology CSI driver to provision Persistent Volumes from my clusters directly on the NAS.

## Secret Management

Azure Key Vaults are used to store my secrets. I sync them to my cluster using the External Secrets Operator.
