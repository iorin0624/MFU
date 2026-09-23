"""Add unique social identities and versioned legal documents.

Revision ID: 0009_identity_legal
Revises: 0008_season_privacy_settings
Create Date: 2026-09-24
"""

import hashlib

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0009_identity_legal"
down_revision = "0008_season_privacy_settings"
branch_labels = None
depends_on = None

TERMS = """# INPA 利用規約

発効日: 2026年9月24日

## 1. 適用
本規約は、MFUが提供するINPA（以下「本サービス」）の利用条件を定めます。

## 2. アカウント
利用者は正確な情報を登録し、認証情報を自己の責任で管理します。アカウントの譲渡・貸与は禁止します。

## 3. サービス内容
本サービスは、テーマパークへの来園予定、プロフィールおよび公開範囲を登録・共有する機能を提供します。公開設定および共有URLの管理は利用者の責任で行います。

## 4. 禁止事項
法令違反、第三者へのなりすまし、権利侵害、迷惑行為、不正アクセス、サービス運営を妨害する行為を禁止します。

## 5. 停止・変更
運営上または安全上必要な場合、本サービスの全部または一部を変更・停止し、違反アカウントを制限できます。

## 6. 免責
本サービスに登録された予定や利用者間のやり取りについて、MFUは可能な範囲で安定運用に努めますが、完全性・正確性・継続性を保証しません。

## 7. 規約の変更
重要な変更では再同意を求めます。変更後の規約は表示された発効日から適用します。
"""

PRIVACY = """# INPA プライバシーポリシー

発効日: 2026年9月24日

## 1. 取得する情報
メールアドレス、表示名、X・Instagram ID、来園予定、公開設定、つながり情報、認証・操作・アクセスに関するログを取得します。

## 2. 利用目的
本人確認、アカウント管理、予定共有、通知、安全対策、不正利用防止、障害調査、サービス改善のために利用します。

## 3. 公開と共有
プロフィールおよび予定は、利用者が選択した公開範囲に従って表示します。共有URLを知る第三者が閲覧できる情報について、利用者は公開設定を確認してください。

## 4. 第三者提供
法令に基づく場合を除き、本人の同意なく個人情報を第三者へ提供しません。サービス運用に必要な委託先には適切な管理を求めます。

## 5. 保存と安全管理
情報は利用目的に必要な期間保持し、アクセス制御、暗号化、監査ログ等の合理的な安全管理措置を講じます。退会後も法令対応や不正利用防止のため必要な記録を一定期間保持する場合があります。

## 6. 確認・訂正・削除
利用者はプロフィールの確認・訂正、退会手続きを行えます。その他の請求はMFU管理者へお問い合わせください。

## 7. Cookie
ログイン状態の維持、CSRF対策および安全なサービス提供のためCookieを使用します。

## 8. 改定
重要な変更では再同意を求めます。変更後のポリシーは表示された発効日から適用します。
"""


def _insert_document(document_type: str, public_id: str, title: str, content: str) -> None:
    op.execute(sa.text(
        "INSERT INTO legal_documents "
        "(public_id,document_type,version,title,content_markdown,content_sha256,status,"
        "requires_reconsent,effective_at,published_at,published_by) "
        "VALUES (:public_id,:document_type,'2026-09-24-v1',:title,:content,:digest,'published',"
        "1,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6),'migration')"
    ).bindparams(
        public_id=public_id, document_type=document_type, title=title, content=content,
        digest=hashlib.sha256(content.encode("utf-8")).hexdigest(),
    ))


def upgrade() -> None:
    op.add_column("users", sa.Column("x_handle_normalized", sa.String(15), nullable=True))
    op.add_column("users", sa.Column("instagram_handle_normalized", sa.String(30), nullable=True))
    op.execute(sa.text(
        "UPDATE users SET x_handle_normalized=LOWER(x_handle) WHERE x_handle IS NOT NULL"
    ))
    op.execute(sa.text(
        "UPDATE users SET instagram_handle_normalized=LOWER(instagram_handle) "
        "WHERE instagram_handle IS NOT NULL"
    ))
    op.create_unique_constraint("uq_users_x_handle_normalized", "users", ["x_handle_normalized"])
    op.create_unique_constraint(
        "uq_users_instagram_handle_normalized", "users", ["instagram_handle_normalized"]
    )

    op.create_table(
        "legal_documents",
        sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column("public_id", mysql.CHAR(26, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column("document_type", sa.String(16), nullable=False),
        sa.Column("version", sa.String(40), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("content_markdown", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column("content_sha256", mysql.CHAR(64, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("requires_reconsent", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("effective_at", sa.DateTime(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("published_by", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.UniqueConstraint("public_id", name="uq_legal_documents_public_id"),
        sa.UniqueConstraint("document_type", "version", name="uq_legal_documents_type_version"),
        sa.CheckConstraint("document_type IN ('terms','privacy')", name="chk_legal_document_type"),
        sa.CheckConstraint("status IN ('draft','published','retired')", name="chk_legal_document_status"),
    )
    op.create_index(
        "ix_legal_documents_current", "legal_documents",
        ["document_type", "status", "effective_at"],
    )
    op.create_table(
        "user_legal_consents",
        sa.Column("user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("legal_document_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("consented_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column("consent_method", sa.String(32), nullable=False),
        sa.Column("ip_address", sa.LargeBinary(16), nullable=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE", name="fk_legal_consent_user"),
        sa.ForeignKeyConstraint(["legal_document_id"], ["legal_documents.id"], ondelete="RESTRICT", name="fk_legal_consent_document"),
        sa.PrimaryKeyConstraint("user_id", "legal_document_id", name="pk_user_legal_consents"),
    )
    _insert_document("terms", "LEGALTERMS20260924V000001", "INPA 利用規約", TERMS)
    _insert_document("privacy", "LEGALPRIVACY20260924V001", "INPA プライバシーポリシー", PRIVACY)


def downgrade() -> None:
    op.drop_table("user_legal_consents")
    op.drop_index("ix_legal_documents_current", table_name="legal_documents")
    op.drop_table("legal_documents")
    op.drop_constraint("uq_users_instagram_handle_normalized", "users", type_="unique")
    op.drop_constraint("uq_users_x_handle_normalized", "users", type_="unique")
    op.drop_column("users", "instagram_handle_normalized")
    op.drop_column("users", "x_handle_normalized")
