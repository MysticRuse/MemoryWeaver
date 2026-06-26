module.exports = {
  apps: [
    {
      name: 'memoryweaver',
      script: './venv/bin/gunicorn',
      args: '--workers 4 --threads 2 --bind 0.0.0.0:5001 app:app',
      cwd: '/Users/hironmoy/MemoryWeaver/memoryweaver-mvp',
      instances: 1,
      exec_mode: 'fork',
      interpreter: 'none',
      autorestart: true,
      watch: false,
      max_memory_restart: '1G',
      env: {
        FLASK_ENV: 'production'
      }
    }
  ]
};
