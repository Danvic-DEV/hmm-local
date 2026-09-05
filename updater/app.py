#!/usr/bin/env python3
"""
HMM-Local Updater Service
Companion container that handles platform updates by recreating the main container
"""
from flask import Flask, request, jsonify, render_template_string
import docker
import logging
import sys
import time
import os
from datetime import datetime

app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# Initialize Docker client
try:
    docker_client = docker.from_env()
    logger.info("✅ Connected to Docker daemon")
except Exception as e:
    logger.error(f"❌ Failed to connect to Docker: {e}")
    sys.exit(1)

# Auto-deploy setting from environment variable
AUTO_DEPLOY_ENABLED = os.getenv('AUTO_DEPLOY_ENABLED', 'false').lower() == 'true'
logger.info(f"🔧 Auto-deploy enabled: {AUTO_DEPLOY_ENABLED}")


@app.route('/', methods=['GET'])
def index():
    """Simple web UI for updater service"""
    html = '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>HMM-Local Updater</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { font-family: system-ui, -apple-system, sans-serif; max-width: 600px; margin: 50px auto; padding: 20px; background: #f5f5f5; }
            .card { background: white; border-radius: 8px; padding: 30px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            h1 { margin: 0 0 10px 0; color: #333; }
            .status { display: inline-block; padding: 4px 12px; border-radius: 12px; font-size: 14px; font-weight: 500; }
            .status.enabled { background: #dcfce7; color: #166534; }
            .status.disabled { background: #fee2e2; color: #991b1b; }
            .info { margin: 20px 0; padding: 15px; background: #f0f9ff; border-left: 3px solid #0ea5e9; border-radius: 4px; }
            .warning { margin: 20px 0; padding: 15px; background: #fff7ed; border-left: 3px solid #f97316; border-radius: 4px; }
            code { background: #f1f5f9; padding: 2px 6px; border-radius: 3px; font-size: 13px; }
            hr { border: none; border-top: 1px solid #e5e7eb; margin: 20px 0; }
            .footer { margin-top: 20px; padding-top: 20px; border-top: 1px solid #e5e7eb; color: #6b7280; font-size: 14px; }
        </style>
    </head>
    <body>
        <div class="card">
            <h1>🔄 HMM-Local Updater</h1>
            <p style="color: #6b7280; margin: 5px 0 0 0;">Container Update Service</p>
            
            <hr>
            
            <h2 style="font-size: 18px; margin: 20px 0 10px 0;">Auto-Deploy Status</h2>
            <span class="status {{ 'enabled' if auto_deploy else 'disabled' }}">
                {{ '✅ Enabled' if auto_deploy else '❌ Disabled' }}
            </span>
            
            {% if auto_deploy %}
            <div class="info">
                <strong>✅ Automatic deployment is enabled</strong><br>
                When new images are pushed to GHCR, this updater will automatically recreate the container with the latest version.
            </div>
            {% else %}
            <div class="warning">
                <strong>⚠️ Automatic deployment is disabled</strong><br>
                New images will be available but you must manually update via the Platform Updates page.
            </div>
            {% endif %}
            
            <hr>
            
            <h2 style="font-size: 18px; margin: 20px 0 10px 0;">Configuration</h2>
            <p style="color: #6b7280; margin: 5px 0;">To change this setting, update your <code>docker-compose.yml</code>:</p>
            <pre style="background: #1e293b; color: #e2e8f0; padding: 15px; border-radius: 6px; overflow-x: auto; font-size: 13px;">updater:
  image: ghcr.io/danvic-dev/hmm-local-updater:latest
  environment:
    - AUTO_DEPLOY_ENABLED={{ 'false' if auto_deploy else 'true' }}  # Change this
  restart: unless-stopped</pre>
            
            <div class="footer">
                Service: <strong>hmm-local-updater</strong><br>
                Timestamp: {{ timestamp }}
            </div>
        </div>
    </body>
    </html>
    '''
    return render_template_string(html, 
        auto_deploy=AUTO_DEPLOY_ENABLED,
        timestamp=datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
    )


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "service": "hmm-local-updater",
        "auto_deploy_enabled": AUTO_DEPLOY_ENABLED,
        "timestamp": datetime.utcnow().isoformat()
    })


@app.route('/update', methods=['POST'])
def update_container():
    """
    Update a container by recreating it with a new image
    
    POST body:
    {
        "container_name": "hmm-local",
        "new_image": "ghcr.io/danvic-dev/hmm-local:main-abc123"
    }
    """
    try:
        data = request.get_json()
        container_name = data.get('container_name')
        new_image = data.get('new_image')
        
        if not container_name or not new_image:
            return jsonify({
                "success": False,
                "error": "Missing container_name or new_image"
            }), 400
        
        # Check if auto-deploy is enabled
        if not AUTO_DEPLOY_ENABLED:
            logger.info(f"⚠️ Auto-deploy disabled, rejecting update request: {container_name} → {new_image}")
            return jsonify({
                "success": False,
                "auto_deploy_enabled": False,
                "message": "Automatic deployment is disabled. Enable AUTO_DEPLOY_ENABLED in docker-compose.yml to allow automatic updates."
            }), 403
        
        logger.info(f"📦 Update request: {container_name} → {new_image}")
        
        # Step 1: Get current container configuration
        logger.info(f"🔍 Getting configuration for {container_name}")
        try:
            container = docker_client.containers.get(container_name)
        except docker.errors.NotFound:
            return jsonify({
                "success": False,
                "error": f"Container '{container_name}' not found"
            }), 404
        
        # Extract configuration
        config = container.attrs['Config']
        host_config = container.attrs['HostConfig']
        network_settings = container.attrs['NetworkSettings']
        
        # Build run parameters
        run_params = {
            'name': container_name,
            'detach': True,
            'environment': config.get('Env', []),
            'volumes': {},
            'network_mode': host_config.get('NetworkMode', 'bridge'),
            'restart_policy': host_config.get('RestartPolicy', {})
        }
        
        # Extract volumes
        binds = host_config.get('Binds', [])
        for bind in binds:
            parts = bind.split(':')
            if len(parts) >= 2:
                run_params['volumes'][parts[0]] = {
                    'bind': parts[1],
                    'mode': parts[2] if len(parts) > 2 else 'rw'
                }
        
        # Extract network settings (for static IP)
        networks = network_settings.get('Networks', {})
        network_name = None
        ip_address = None
        
        if networks:
            # Get first network (usually there's only one)
            network_name = list(networks.keys())[0]
            network_info = networks[network_name]
            ip_address = network_info.get('IPAddress')
        
        # Step 2: Pull new image FIRST (before stopping container)
        logger.info(f"📥 Pulling image: {new_image}")
        docker_client.images.pull(new_image)
        
        # Step 3: Stop container
        logger.info(f"⏹️  Stopping {container_name}")
        container.stop(timeout=30)
        
        # Step 4: Remove container
        logger.info(f"🗑️  Removing {container_name}")
        container.remove()
        
        # Small delay to ensure cleanup
        time.sleep(1)
        
        # Step 5: Create new container
        logger.info(f"🚀 Starting new container with {new_image}")
        
        # If we have a specific network and IP, handle that
        if network_name and network_name not in ['bridge', 'host', 'none']:
            # Create with network and IP
            networking_config = docker_client.api.create_networking_config({
                network_name: docker_client.api.create_endpoint_config(
                    ipv4_address=ip_address if ip_address else None
                )
            })
            
            new_container = docker_client.api.create_container(
                image=new_image,
                name=container_name,
                environment=run_params['environment'],
                host_config=docker_client.api.create_host_config(
                    binds=binds,
                    restart_policy=run_params['restart_policy'],
                    network_mode=network_name
                ),
                networking_config=networking_config,
                detach=True
            )
            docker_client.api.start(new_container['Id'])
        else:
            # Simple creation (docker-compose style)
            new_container = docker_client.containers.run(
                image=new_image,
                **run_params
            )
        
        logger.info(f"✅ Update completed successfully")
        
        return jsonify({
            "success": True,
            "message": f"Container {container_name} updated to {new_image}",
            "container_id": new_container.id if hasattr(new_container, 'id') else new_container['Id'],
            "timestamp": datetime.utcnow().isoformat()
        })
        
    except Exception as e:
        logger.error(f"❌ Update failed: {e}", exc_info=True)
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


if __name__ == '__main__':
    logger.info("🚀 Starting HMM-Local Updater Service on port 8081")
    app.run(host='0.0.0.0', port=8081)
