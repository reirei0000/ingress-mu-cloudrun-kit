#!/usr/bin/env bash
# =============================================================================
# Ingress MU Calculator - オールインワン自動構築＆Cloud Runデプロイスクリプト
#
# 使い方:
#   bash deploy/cloudrun/deploy.sh
#
# このスクリプト1つで以下をすべて自動で行います:
#   1. GCPプロジェクト・リージョンの対話設定
#   2. Python仮想環境 (.venv) の自動構築
#   3. NASA Earthdata からの人口データ (GRUMP v1) 自動ダウンロード＆展開（未取得の場合）
#   4. S2 Level 6〜8 事前計算テーブル & headers.json の生成（未生成の場合）
#   5. Cloud Storage バケット作成＆データ転送
#   6. Cloud Run へのビルド＆デプロイ（無料枠に収まる安全制限付き）
# =============================================================================
set -euo pipefail
# ブラウザと Cloud Shell の一時的な切断 (SIGHUP) で処理が中断しないようにする
trap '' HUP

cd "$(dirname "$0")/../.."
REPO_ROOT="$(pwd)"

GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }

echo "============================================================"
echo "  Ingress MU Calculator - Cloud Run 自動セットアップ"
echo "============================================================"
echo ""

# -----------------------------------------------------------------------------
# 1. gcloud ログイン確認と GCP プロジェクト ID の取得
# -----------------------------------------------------------------------------
if ! command -v gcloud >/dev/null 2>&1; then
  err "gcloud コマンドが見つかりません。Google Cloud Shell 上で実行するか、Google Cloud SDK をインストールしてください。"
  exit 1
fi

ACTIVE_ACCOUNT="$(gcloud auth list --filter=status:ACTIVE --format="value(account)" 2>/dev/null | head -n 1 || true)"
if [[ -z "${ACTIVE_ACCOUNT}" ]]; then
  info "Google Cloud にログインします..."
  gcloud auth login
fi

CURRENT_PROJ="$(gcloud config get-value project 2>/dev/null || true)"
if [[ "${CURRENT_PROJ}" == "(unset)" ]]; then
  CURRENT_PROJ=""
fi

PROJECT_ID="${GCP_PROJECT_ID:-}"
if [[ -z "${PROJECT_ID}" ]]; then
  echo "使用する GCP プロジェクト ID を入力してください。"
  if [[ -n "${CURRENT_PROJ}" ]]; then
    read -rp "GCP プロジェクト ID [Enter で '${CURRENT_PROJ}' を使用]: " INPUT_PROJ
    PROJECT_ID="${INPUT_PROJ:-${CURRENT_PROJ}}"
  else
    read -rp "GCP プロジェクト ID (例: ingress-mu-123456): " PROJECT_ID
  fi
fi

if [[ -z "${PROJECT_ID}" ]]; then
  err "GCP プロジェクト ID が指定されていません。"
  exit 1
fi
gcloud config set project "${PROJECT_ID}" >/dev/null 2>&1 || true

# -----------------------------------------------------------------------------
# 2. リージョンの選択
# -----------------------------------------------------------------------------
REGION="${GCP_REGION:-}"
if [[ -z "${REGION}" ]]; then
  echo ""
  echo "デプロイ先のリージョンを選択してください:"
  echo "  1) us-west1        (北米オレゴン: GCP常時無料枠の対象・0円運用におすすめ) [デフォルト]"
  echo "  2) asia-northeast1 (東京: 日本から最速・GCS保存代が月約12円かかります)"
  read -rp "番号を入力 [1 または 2, Enter で 1]: " REGION_CHOICE
  case "${REGION_CHOICE:-1}" in
    2) REGION="asia-northeast1" ;;
    *) REGION="us-west1" ;;
  esac
fi

BUCKET="${GCS_BUCKET:-mu-calculator-data-${PROJECT_ID}}"
SERVICE_NAME="${CLOUD_RUN_SERVICE:-mu-calculator}"

# ホームディレクトリに 3.6GB 以上の空きがあれば消えないホーム領域を使い、足りない場合のみ /tmp を使う
DEFAULT_DATA_DIR="mu_calculator/data/grump"
if [[ -d "${DEFAULT_DATA_DIR}/bil" ]]; then
  DATA_DIR="${GRUMP_DATA:-${DEFAULT_DATA_DIR}}"
else
  FREE_KB="$(df -Pk "${REPO_ROOT}" | awk 'NR==2 {print $4}')"
  if [[ "${FREE_KB:-10000000}" -lt 3600000 ]]; then
    warn "ホーム領域の空き容量が 3.6GB 未満のため、作業用データ保存先に /tmp/grump_data を使用します。"
    DATA_DIR="${GRUMP_DATA:-/tmp/grump_data}"
  else
    DATA_DIR="${GRUMP_DATA:-${DEFAULT_DATA_DIR}}"
  fi
fi
mkdir -p "${DATA_DIR}"

echo ""
echo "------------------------------------------------------------"
echo "  GCP プロジェクト : ${PROJECT_ID}"
echo "  リージョン       : ${REGION}"
echo "  Storage バケット : gs://${BUCKET}"
echo "  Cloud Run 名     : ${SERVICE_NAME}"
echo "  ローカルデータ先 : ${DATA_DIR}"
echo "------------------------------------------------------------"
echo ""

# -----------------------------------------------------------------------------
# 3. Python 仮想環境 (.venv) の準備
# -----------------------------------------------------------------------------
VENV_PYTHON="${REPO_ROOT}/.venv/bin/python"
if [[ ! -x "${VENV_PYTHON}" ]]; then
  info "[Step 1/6] Python 仮想環境 (.venv) を作成し、必要なライブラリをインストールしています..."
  python3 -m venv "${REPO_ROOT}/.venv"
  "${REPO_ROOT}/.venv/bin/pip" install --upgrade pip >/dev/null
  "${REPO_ROOT}/.venv/bin/pip" install -r "${REPO_ROOT}/requirements.txt"
else
  ok "[Step 1/6] Python 仮想環境 (.venv) は準備済みです。"
fi

# -----------------------------------------------------------------------------
# 4. Cloud Storage にすでにデータがあるか確認（あれば Step 2〜5 をスキップ可能）
# -----------------------------------------------------------------------------
GCS_READY=0
if gcloud storage ls "gs://${BUCKET}/density-l6.json" --project="${PROJECT_ID}" >/dev/null 2>&1 && [[ "${FORCE_SYNC:-0}" != "1" ]]; then
  GCS_READY=1
  ok "Cloud Storage (gs://${BUCKET}) にデータが既に存在するため、ダウンロードとテーブル生成をスキップします！"
fi

# -----------------------------------------------------------------------------
# 5. NASA Earthdata からの人口データ (GRUMP v1) ダウンロード＆展開
# -----------------------------------------------------------------------------
HDR_COUNT=0
if [[ -d "${DATA_DIR}/bil" ]]; then
  HDR_COUNT="$(find "${DATA_DIR}/bil" -maxdepth 1 -name "*up00ag.hdr" | wc -l | tr -d '[:space:]')"
fi

if [[ "${GCS_READY}" -eq 0 ]] && [[ "${HDR_COUNT}" -lt 234 ]]; then
  echo ""
  info "[Step 2/6] 人口データ (GRUMP v1 全234カ国) を NASA Earthdata からダウンロードします。"
  echo "  ※まだアカウントをお持ちでない場合は https://urs.earthdata.nasa.gov/ で無料登録し、"
  echo "    Applications > Authorized Apps > Approve More Applications で"
  echo "    'SEDAC' / 'NASA GESDISC DATA ARCHIVE' を承認してください。"
  echo ""
  while true; do
    if [[ -z "${EARTHDATA_USERNAME:-}" ]]; then
      read -rp "NASA Earthdata ユーザー名: " EARTHDATA_USERNAME
      export EARTHDATA_USERNAME
    fi
    if [[ -z "${EARTHDATA_PASSWORD:-}" ]]; then
      read -rsp "NASA Earthdata パスワード (入力文字は表示されません): " EARTHDATA_PASSWORD
      echo ""
      export EARTHDATA_PASSWORD
    fi
    if "${VENV_PYTHON}" -m mu_calculator.download --directory "${DATA_DIR}"; then
      break
    fi
    warn "ダウンロードに失敗しました。ユーザー名・パスワード、または Earthdata の Authorized Apps 承認設定を確認して再入力してください。"
    EARTHDATA_USERNAME=""
    EARTHDATA_PASSWORD=""
  done
  ok "全234カ国のラスタデータのダウンロードと展開が完了しました。"
else
  ok "[Step 2/6] ラスタデータの準備は完了済みです。"
fi

# -----------------------------------------------------------------------------
# 6. 事前計算テーブルと headers.json の生成
# -----------------------------------------------------------------------------
CPU_CORES="$(nproc 2>/dev/null || echo 2)"
if [[ -z "${MU_TABLE_MAX_LEVEL:-}" ]]; then
  if [[ "${CPU_CORES}" -le 4 ]]; then
    TABLE_MAX=6
  else
    TABLE_MAX=8
  fi
else
  TABLE_MAX="${MU_TABLE_MAX_LEVEL}"
fi
if [[ "${GCS_READY}" -eq 0 ]]; then
  if [[ ! -f "${DATA_DIR}/density-l6.json" ]] || [[ ! -f "${DATA_DIR}/density-l${TABLE_MAX}.json" ]]; then
    info "[Step 3/6] S2 Level 6〜${TABLE_MAX} の事前計算テーブルを生成しています（初回のみ数分〜10分ほどかかります）..."
    GRUMP_DATA="${DATA_DIR}" MU_TABLE_MAX_LEVEL="${TABLE_MAX}" "${VENV_PYTHON}" -m mu_calculator build-tables
    ok "事前計算テーブルの生成が完了しました。"
  else
    ok "[Step 3/6] 事前計算テーブル (Level 6〜${TABLE_MAX}) は生成済みです。"
  fi

  if [[ ! -f "${DATA_DIR}/headers.json" ]]; then
    GRUMP_DATA="${DATA_DIR}" "${VENV_PYTHON}" deploy/cloudrun/entrypoint.py build-headers
  fi
else
  ok "[Step 3/6] 事前計算テーブルは Cloud Storage 上に準備済みです。"
fi

# -----------------------------------------------------------------------------
# 6. GCP API の有効化と Cloud Storage バケットへのデータ転送
# -----------------------------------------------------------------------------
info "[Step 4/6] Google Cloud の必要な API (Cloud Run / Cloud Build / Storage) を有効化しています..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  storage.googleapis.com \
  --project="${PROJECT_ID}"

info "[Step 5/6] Cloud Storage バケット gs://${BUCKET} を準備しています..."
if ! gcloud storage buckets describe "gs://${BUCKET}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUCKET}" \
    --project="${PROJECT_ID}" \
    --location="${REGION}" \
    --uniform-bucket-level-access
fi

# 新規作成された GCP プロジェクトでも Cloud Run がバケットを読めるよう権限を付与
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)" 2>/dev/null || true)"
if [[ -n "${PROJECT_NUMBER}" ]]; then
  gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
    --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
    --role="roles/storage.objectViewer" \
    --project="${PROJECT_ID}" >/dev/null 2>&1 || true
fi

if gcloud storage ls "gs://${BUCKET}/density-l6.json" --project="${PROJECT_ID}" >/dev/null 2>&1 && [[ "${FORCE_SYNC:-0}" != "1" ]]; then
  ok "gs://${BUCKET} にデータはアップロード済みです（スキップします。再転送する場合は FORCE_SYNC=1 を指定）"
else
  info "事前計算テーブル・headers.json・bil/ ラスタ (約3.2GB) を gs://${BUCKET} へアップロードしています..."
  gcloud storage cp "${DATA_DIR}/headers.json" "${DATA_DIR}"/density-l*.json "${DATA_DIR}"/density-l*.npy "gs://${BUCKET}/" --project="${PROJECT_ID}"
  gcloud storage rsync "${DATA_DIR}/bil" "gs://${BUCKET}/bil" --project="${PROJECT_ID}"
  ok "Cloud Storage へのデータアップロードが完了しました。"
fi

# -----------------------------------------------------------------------------
# 7. Cloud Run へのビルド＆デプロイ
# -----------------------------------------------------------------------------
info "[Step 6/6] Cloud Run (${SERVICE_NAME}) へデプロイしています（2〜3分ほどかかります）..."
cp Dockerfile Dockerfile.orig.bak
cp deploy/cloudrun/Dockerfile.cloudrun Dockerfile
trap 'mv -f Dockerfile.orig.bak Dockerfile' EXIT

gcloud run deploy "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --source=. \
  --execution-environment=gen2 \
  --cpu=1 \
  --memory=512Mi \
  --min-instances=0 \
  --max-instances=1 \
  --concurrency=10 \
  --timeout=30 \
  --set-env-vars="GRUMP_DATA=/data/grump,MU_FAST_STARTUP=1,MU_TABLE_MAX_LEVEL=${TABLE_MAX},MU_TRUSTED_PROXY_HEADERS=1" \
  --add-volume="name=grump-data,type=cloud-storage,bucket=${BUCKET},readonly=true" \
  --add-volume-mount="volume=grump-data,mount-path=/data/grump" \
  --allow-unauthenticated

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" --project="${PROJECT_ID}" --region="${REGION}" --format="value(status.url)" 2>/dev/null || true)"

echo ""
echo "============================================================"
ok "デプロイがすべて完了しました！！"
echo "============================================================"
if [[ -n "${SERVICE_URL}" ]]; then
  echo ""
  echo "  あなたの API URL      : ${SERVICE_URL}"
  echo "  Swagger テスト画面    : ${SERVICE_URL}/docs"
  echo "  ブラウザ計算テスト例  : ${SERVICE_URL}/getMU?c=35.658581,139.745433&c=35.710063,139.810700&c=35.689487,139.691706"
  echo ""
fi
