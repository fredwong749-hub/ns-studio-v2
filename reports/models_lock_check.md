# models.lock 校验（M9，只读）

| LoRA | 锁表 sha256 | gpu33 | gpu36 |
|---|---|---|---|
| jewellery_nprng2_v1.safetensors | null | 缺 | 缺 |
| jewellery_lbrng9_v1.safetensors | null | 缺 | 缺 |
| jewellery_lbrng9_beads_v1.safetensors | null | 缺 | 缺 |
| wardrobe_heels_v1.safetensors | null | 缺 | 缺 |
| wardrobe_clothes_v1.safetensors | null | 缺 | 缺 |
| lin_wan_h3_v1.safetensors | null | 未校验 `304f4c02a4f3` | 缺 |
| nicegirls_v2_qwen21.safetensors | `8ea63de35919` | 缺 | 缺 |
| purelens_qwen21.safetensors | `386c7f620932` | 缺 | 缺 |
| qwen21-t2r-v1.safetensors | `ccbf244c7558` | 缺 | 缺 |
| Qwen2.1_Anything2RealCharacters.safetensors | `5db4dd2376e6` | 缺 | 缺 |
| purelens_qwen21.safetensors | `57753c6f9dc2` | 缺 | 缺 |

## 结论

- 未校验（锁表 sha256 = null）：6（jewellery_lbrng9_beads_v1.safetensors, jewellery_lbrng9_v1.safetensors, jewellery_nprng2_v1.safetensors, lin_wan_h3_v1.safetensors, wardrobe_clothes_v1.safetensors, wardrobe_heels_v1.safetensors）
- 不一致：0
- 缺文件：21（gpu33:Qwen2.1_Anything2RealCharacters.safetensors, gpu33:jewellery_lbrng9_beads_v1.safetensors, gpu33:jewellery_lbrng9_v1.safetensors, gpu33:jewellery_nprng2_v1.safetensors, gpu33:nicegirls_v2_qwen21.safetensors, gpu33:purelens_qwen21.safetensors, gpu33:purelens_qwen21.safetensors, gpu33:qwen21-t2r-v1.safetensors, gpu33:wardrobe_clothes_v1.safetensors, gpu33:wardrobe_heels_v1.safetensors, gpu36:Qwen2.1_Anything2RealCharacters.safetensors, gpu36:jewellery_lbrng9_beads_v1.safetensors, gpu36:jewellery_lbrng9_v1.safetensors, gpu36:jewellery_nprng2_v1.safetensors, gpu36:lin_wan_h3_v1.safetensors, gpu36:nicegirls_v2_qwen21.safetensors, gpu36:purelens_qwen21.safetensors, gpu36:purelens_qwen21.safetensors, gpu36:qwen21-t2r-v1.safetensors, gpu36:wardrobe_clothes_v1.safetensors, gpu36:wardrobe_heels_v1.safetensors）
- 重复登记（同名不同 sha256，gpu33 读到的锁表）：purelens_qwen21.safetensors → ['386c7f6209327ec1337da055e77041452d314962d65ed7efeb920218be4330da', '57753c6f9dc29fac53d0713bd67e3ffee24447dac0cbf3af8f1dff8b6008d3dd']
- gpu33 目录里有、锁表里没有：H3-Licon-VBVR-I2V.safetensors, Motion_Repair_V2.safetensors, QI2.1_AnyAngle.safetensors, krea2_film_texture_qwen21.safetensors, minimax_h3_fl2v_turbo_8step_v1.1_768p_comfyui_bf16.safetensors, minimax_h3_ref2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors, minimax_h3_ref2v_turbo_8step_v1.1_768p_comfyui_bf16.safetensors, moss_image_edit_lora_rank_64_bf16.safetensors, pika_h3_flex_fl2va_comfy_bf16.safetensors, qwen-image-2.1-outfit-swap.safetensors
- 重复登记（同名不同 sha256，gpu36 读到的锁表）：purelens_qwen21.safetensors → ['386c7f6209327ec1337da055e77041452d314962d65ed7efeb920218be4330da', '57753c6f9dc29fac53d0713bd67e3ffee24447dac0cbf3af8f1dff8b6008d3dd']
- gpu36 目录里有、锁表里没有：FireRed-Image-Edit-1.1-Lightning-8steps-v1.2.safetensors, Motion_Repair_V2.safetensors, QI2.1_AnyAngle.safetensors, Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors, camera_motion_h3_lora_v1_3000_pruned.safetensors, krea2_film_texture_qwen21.safetensors, krea2_general_edit_v2.safetensors, krea2_identity_edit_v2.safetensors, krea2_turbo_lora_rank_64_bf16.safetensors, minimax_h3_fl2v_turbo_8step_v1.1_768p_comfyui_bf16.safetensors, minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors, moss_image_edit_lora_rank_64_bf16.safetensors, pika_h3_flex_fl2va_comfy_bf16.safetensors

只报告、不拦渲染；是否在渲染前强制校验由项目方定。
