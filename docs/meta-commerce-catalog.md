# Sync a Meta Commerce catalog for WhatsApp

A Meta Commerce catalog stores the products WhatsApp displays in native shopping messages. Syncing a catalog is a project-owned extension, not a SMART feature: SMART does not create catalogs, publish feeds, or provide native catalog-message builders.

## When you need a catalog

Native single-product (`product`), multi-product (`product_list`), and catalog messages need a catalog with products linked to the WhatsApp Business Account (WABA) used by the sending number. Set up the catalog in Commerce Manager, then connect it to that WABA. Keep the catalog ID and each product's Content ID: a native product message uses the latter as `product_retailer_id`, not the catalog item's Graph API ID. See Meta's [guide to sharing products](https://developers.facebook.com/docs/whatsapp/guides/commerce-guides/share-products-with-customers/) and [WABA catalog reference](https://developers.facebook.com/docs/graph-api/reference/whats-app-business-account/product_catalogs/).

If customers only need to see a product and follow a link, ordinary WhatsApp image messages and link buttons do not need a catalog. You can build a project-owned product card with the existing [rich-messages recipe](../recipes/rich-messages/README.md). It does not open WhatsApp's native product detail or cart.

## Run a scheduled feed outside SMART

1. **Export from the source of truth.** On a schedule, read the storefront or inventory system with a read-only credential. Export only listings approved for this channel.
2. **Normalize and validate.** Map each saleable item or variant to a stable feed `id`. Include the title, description, product page URL, direct image URL, availability, condition, and public price with currency. Use Meta's [Product Item examples](https://developers.facebook.com/docs/marketing-api/reference/product-item/) and the feed template for your catalog type to check field names, accepted values, and any extra required fields. Confirm that Meta can fetch each image without a storefront login and that the images meet the catalog's requirements.
3. **Publish a complete feed.** Publish a validated file at a stable HTTPS URL and configure Commerce Manager to fetch it after your export runs. Use feed authentication if supported; keep the source export private and keep feed credentials out of source code, agent prompts, and logs. If the fetch must be public, use a hard-to-guess URL as an extra precaution, not as access control. Never include unpublished prices, internal stock counts, customer data, or credentials in the file.
4. **Check the import.** Keep the last known-good feed if the upstream export is empty, incomplete, or invalid. Meta's [scheduled full feed](https://developers.facebook.com/docs/graph-api/reference/product-feed/) can remove items omitted from the next file. Review import errors and item counts in Commerce Manager, and confirm a sample item appears under the linked WABA before relying on the catalog in messages.
